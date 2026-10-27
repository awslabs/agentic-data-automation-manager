#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Which hosts a function inside the VPC may be told to dial."""

import ipaddress
import socket

from .http_response import Rejected


def validate_target(host: str) -> None:
    """Refuse hosts the provider function must not be sent to dial.

    The provider runs inside the VPC and logs in to whatever host it is given, so
    the host a user types is where the function connects. Loopback, link-local
    (the instance metadata address lives there), unspecified, multicast and
    reserved ranges are never a customer database. Private ranges are allowed:
    that is where databases are.
    """
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        raise Rejected(f"Cannot resolve host {host!r}.") from None
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if (ip.is_loopback or ip.is_link_local or ip.is_unspecified
                or ip.is_multicast or ip.is_reserved):
            raise Rejected(f"{host!r} is not an address a database can be registered at.")
