"""Tests for cloud-init NoCloud seed building and verification.

A NoCloud seed that is subtly wrong fails silently: the guest boots, the
reader rejects the seed, and cloud-init falls back to DataSourceNone, so the
machine waits at an interactive wizard nobody is there to answer. These tests
pin the two properties that actually matter -- the cidata label and long
filenames surviving -- so a regression is caught here rather than one boot
cycle later.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vm_harness.cloudinit import (  # noqa: E402
    DEFAULT_LABEL,
    NoCloudSeed,
    SeedBuilder,
    SeedError,
    verify_seed,
)

pycdlib = pytest.importorskip("pycdlib")

OMARCHY_FILES = {
    "user_configuration.json": '{"hostname": "omarchy-vm", "timezone": "America/New_York"}',
    "user_credentials.json": '{"username": "OmarchyVM", "password_hash": "$6$abc$def"}',
    "user_full_name.txt": "Omarchy VM User\n",
    "user_email_address.txt": "someone@example.com\n",
}


def _omarchy_seed(**kw) -> NoCloudSeed:
    return NoCloudSeed(
        hostname="omarchy-vm",
        timezone="America/New_York",
        extra_files=dict(OMARCHY_FILES),
        required_files=tuple(OMARCHY_FILES),
        **kw,
    )


class TestSeedContents:
    def test_meta_data_always_written(self):
        """cloud-init requires meta-data even with no hostname set."""
        assert "meta-data" in NoCloudSeed().files()

    def test_user_data_always_written(self):
        """cloud-init requires user-data.

        This is the rule that made an installer-specific-only seed invalid:
        NoCloud sets required=["user-data", "meta-data"], so a device carrying
        just user_configuration.json and friends is rejected as
        "not a valid seed" and the guest falls back to DataSourceNone.
        """
        files = NoCloudSeed(extra_files={"user_configuration.json": "{}"}).files()
        assert "user-data" in files
        assert files["user-data"].startswith(b"#cloud-config")

    def test_timezone_recorded_in_user_data(self):
        files = NoCloudSeed(hostname="box", timezone="America/New_York").files()
        assert b"timezone: America/New_York" in files["user-data"]

    def test_meta_data_written_when_hostname_set(self):
        files = NoCloudSeed(hostname="box").files()
        assert b"local-hostname: box" in files["meta-data"]

    def test_user_data_is_valid_cloud_config(self):
        files = NoCloudSeed(hostname="box", username="omarchyvm").files()
        text = files["user-data"].decode()
        assert text.startswith("#cloud-config")
        assert "user: omarchyvm" in text

    def test_password_hash_not_written_when_absent(self):
        """A username alone must not produce a bogus passwd entry.

        Checks the whole line: "lock_passwd:" also contains "passwd:".
        """
        files = NoCloudSeed(username="omarchyvm").files()
        lines = files["user-data"].decode().splitlines()
        assert not any(line.strip().startswith("passwd:") for line in lines)

    def test_authorized_keys_rendered(self):
        files = NoCloudSeed(username="u", authorized_keys=["ssh-ed25519 AAAA k"]).files()
        assert "ssh-ed25519 AAAA k" in files["user-data"].decode()
        assert files["authorized_keys"].strip() == b"ssh-ed25519 AAAA k"

    def test_encrypt_flag_written_only_when_set(self):
        assert "user_encrypt_installation" not in NoCloudSeed().files()
        assert NoCloudSeed(encrypt=True).files()["user_encrypt_installation"] == b"true\n"

    def test_extra_files_included(self):
        files = NoCloudSeed(extra_files={"a.txt": "hello"}).files()
        assert files["a.txt"] == b"hello"


class TestBuild:
    def test_builds_and_verifies(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_omarchy_seed(), out)
        assert out.exists()
        assert verify_seed(out, OMARCHY_FILES)

    def test_volume_label_is_cidata(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_omarchy_seed(), out)
        data = out.read_bytes()
        label = data[16 * 2048 + 40:16 * 2048 + 72].decode("ascii").strip()
        assert label.upper() == DEFAULT_LABEL.upper()

    def test_long_filenames_survive(self, tmp_path):
        """The failure this whole module exists to prevent: names must not be
        reduced to 8.3 form, or a NoCloud reader cannot find them."""
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_omarchy_seed(), out)
        blob = out.read_bytes()
        for name in OMARCHY_FILES:
            assert name.encode() in blob, f"{name} missing from the image"

    def test_joliet_descriptor_present(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_omarchy_seed(), out)
        data = out.read_bytes()
        joliet = [
            s for s in range(17, 24)
            if data[s * 2048 + 1:s * 2048 + 6] == b"CD001" and data[s * 2048] == 2
        ]
        assert joliet, "no supplementary volume descriptor: long names would be lost"

    def test_json_payloads_survive_verbatim(self, tmp_path):
        out = tmp_path / "cidata.iso"
        seed = _omarchy_seed()
        SeedBuilder().build(seed, out)
        assert seed.extra_files["user_credentials.json"].encode() in out.read_bytes()

    def test_parent_directory_created(self, tmp_path):
        out = tmp_path / "nested" / "deeper" / "cidata.iso"
        SeedBuilder().build(_omarchy_seed(), out)
        assert out.exists()


class TestVerification:
    def test_missing_file_raises(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_omarchy_seed(), out)
        with pytest.raises(SeedError, match="missing from seed"):
            verify_seed(out, ["not_present.txt"])

    def test_missing_file_rejected(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_omarchy_seed(), out)
        with pytest.raises(SeedError):
            verify_seed(out, ["authorized_keys"])

    def test_seed_missing_user_data_rejected(self, tmp_path):
        """An image without cloud-init's required user-data must not verify.

        Reproduces the real failure: a structurally perfect ISO labelled cidata
        that the guest still refused, because NoCloud requires both documents.
        """
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_omarchy_seed(), out)
        data = bytearray(out.read_bytes())
        # Rename the Joliet/RR name so the file is genuinely absent.
        broken = bytes(data).replace(b"user-data", b"USER_DATX")
        b2 = tmp_path / "nouserdata.iso"
        b2.write_bytes(broken)
        with pytest.raises(SeedError, match="user-data"):
            verify_seed(b2)

    def test_absent_image_raises(self, tmp_path):
        with pytest.raises(SeedError, match="not found"):
            verify_seed(tmp_path / "nope.iso")

    def test_tiny_file_rejected(self, tmp_path):
        bad = tmp_path / "bad.iso"
        bad.write_bytes(b"x" * 100)
        with pytest.raises(SeedError, match="too small"):
            verify_seed(bad)

    def test_non_iso_rejected(self, tmp_path):
        """A file with no PVD must not pass as a seed."""
        bad = tmp_path / "bad.iso"
        bad.write_bytes(b"\x00" * (2048 * 20))
        with pytest.raises(SeedError, match="primary volume descriptor"):
            verify_seed(bad)

    def test_wrong_label_rejected(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(NoCloudSeed(hostname="box"), out)
        data = bytearray(out.read_bytes())
        # Overwrite the label field so the descriptor keeps its structure.
        label = b"OTHERLABEL".ljust(32, b" ")
        data[16 * 2048 + 40:16 * 2048 + 72] = label
        broken = tmp_path / "broken.iso"
        broken.write_bytes(bytes(data))
        with pytest.raises(SeedError, match="volume label"):
            verify_seed(broken)

    def test_iso_without_joliet_rejected(self, tmp_path):
        """This is the exact shape of the original bug: a structurally valid
        ISO whose only names are 8.3."""
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(NoCloudSeed(hostname="box"), out)
        data = bytearray(out.read_bytes())
        # Remove the Joliet SVD by turning sector 17 into the terminator.
        data[17 * 2048] = 255
        broken = tmp_path / "nojoliet.iso"
        broken.write_bytes(bytes(data))
        with pytest.raises(SeedError, match="Joliet"):
            verify_seed(broken)


class TestBackends:
    def test_pycdlib_available_in_test_env(self):
        assert SeedBuilder()._available("pycdlib") is True

    def test_auto_order_prefers_pycdlib(self):
        order = SeedBuilder()._backend_order()
        assert order[0] == "pycdlib"

    def test_describe_reports_availability(self):
        described = SeedBuilder().describe()
        assert "pycdlib" in described
        assert described["pycdlib"] is True

    def test_unknown_backend_fails_loudly(self, tmp_path):
        with pytest.raises(SeedError):
            SeedBuilder("nonsense").build(_omarchy_seed(), tmp_path / "x.iso")

    def test_imapi2_backend_refuses_with_guidance(self, tmp_path):
        """The Windows COM fallback must explain itself rather than emit an
        image NoCloud readers reject."""
        builder = SeedBuilder("imapi2")
        if not builder._available("imapi2"):
            pytest.skip("not on Windows")
        with pytest.raises(SeedError, match="pycdlib"):
            builder.build(_omarchy_seed(), tmp_path / "x.iso")


class TestConvenience:
    def test_build_seed_helper(self, tmp_path):
        out = tmp_path / "cidata.iso"
        from vm_harness.cloudinit import build_seed

        build_seed(out, hostname="box", extra_files={"a.json": "{}"})
        assert out.exists()
        verify_seed(out, ["a.json"])