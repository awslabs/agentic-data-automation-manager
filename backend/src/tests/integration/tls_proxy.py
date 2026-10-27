#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A TLS listener in front of a plain one, with its own CA: a database that offers TLS.

The test Oracle container has no TCPS listener. This terminates TLS for `localhost`
with a certificate from a CA made here, and passes the bytes to the container, so
the real driver and the real database are on either side of a real handshake. A
client that does not speak TLS to it gets nothing.
"""

import datetime
import socket
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID


@dataclass
class TlsListener:
    host: str
    port: int
    ca_file: str
    certificate: str  # what it presents, PEM
    handshakes: list = field(default_factory=list)  # one entry per completed TLS handshake


def _self_signed(directory: Path) -> tuple[Path, Path, Path]:
    """As dev's Oracle presents on its TCPS port: self-signed X.509 version 1 (no extensions at
    all, so no CA markings and no alternative names), RSA, the host as its common name. No CA
    bundle verifies it, and strict verification refuses it even as a trust anchor."""
    now = datetime.datetime.now(datetime.UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
               .serial_number(3).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)))
    builder._version = x509.Version.v1  # the library builds only v3 publicly
    cert = builder.sign(key, hashes.SHA256())
    paths = directory / "self-ca.pem", directory / "self-cert.pem", directory / "self-key.pem"
    paths[0].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return paths


def _certificates(directory: Path) -> tuple[Path, Path, Path]:
    now = datetime.datetime.now(datetime.UTC)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    paths = directory / "ca.pem", directory / "cert.pem", directory / "key.pem"
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return paths


def _pump(source, sink):
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for s in (source, sink):
            try:
                s.close()
            except OSError:
                pass


@contextmanager
def tls_listener(upstream_host: str, upstream_port: int, directory: Path, *, self_signed: bool = False):
    """`self_signed`: present a certificate of its own, as dev's Oracle does, instead of one from the test CA."""
    ca_file, cert_file, key_file = (_self_signed if self_signed else _certificates)(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server = socket.create_server(("127.0.0.1", 0))
    listener = TlsListener("localhost", server.getsockname()[1], str(ca_file), cert_file.read_text())

    def serve():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return  # closed
            try:
                wrapped = context.wrap_socket(client, server_side=True)
            except (ssl.SSLError, OSError):
                client.close()
                continue
            listener.handshakes.append(wrapped.version())
            upstream = socket.create_connection((upstream_host, upstream_port))
            threading.Thread(target=_pump, args=(wrapped, upstream), daemon=True).start()
            threading.Thread(target=_pump, args=(upstream, wrapped), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield listener
    finally:
        server.close()
