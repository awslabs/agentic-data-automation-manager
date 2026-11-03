#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""AuthProviderStack loads the identity provider modules beside it by file name, and only those."""

import sys

import pytest

from cdk_infra.auth_provider.auth_provider_stack import identity_provider_modules


@pytest.fixture
def package(tmp_path, monkeypatch):
    folder = tmp_path / "sign_in_pkg"
    folder.mkdir()
    (folder / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield folder
    for name in [n for n in sys.modules if n.startswith("sign_in_pkg")]:
        del sys.modules[name]


def test_only_files_named_for_an_identity_provider_are_loaded(package):
    for name in ("b_identity_provider.py", "a_identity_provider.py", "helpers.py",
                 "identity_provider_notes.py", "x_identity_provider.py.bak"):
        (package / name).write_text("LOADED = True\n")
    modules = identity_provider_modules(package, "sign_in_pkg")
    assert [m.__name__ for m in modules] == ["sign_in_pkg.a_identity_provider", "sign_in_pkg.b_identity_provider"]
    assert "sign_in_pkg.helpers" not in sys.modules


def test_a_folder_without_one_loads_nothing(package):
    (package / "auth_provider_stack.py").write_text("")
    assert identity_provider_modules(package, "sign_in_pkg") == []


def test_a_module_that_fails_to_import_stops_synth(package):
    (package / "broken_identity_provider.py").write_text("raise RuntimeError('misconfigured')\n")
    with pytest.raises(RuntimeError, match="misconfigured"):
        identity_provider_modules(package, "sign_in_pkg")
