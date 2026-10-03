"""Tests for cloud-init NoCloud seed building and verification.

A NoCloud seed that is subtly wrong fails silently: the guest boots, the
reader rejects the seed, and cloud-init falls back to DataSourceNone, so the
machine waits at an interactive wizard nobody is there to answer. These tests
pin the properties that actually matter -- the cidata label, long filenames
surviving, and the *filenames actually present in the image's directory
records* -- so a regression is caught here rather than one boot cycle later.

The independent-parse tests at the end go further and hand a built image to
Windows itself (``Mount-DiskImage``), so nothing in this file is graded on the
same assumption twice.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vm_harness.cloudinit import (  # noqa: E402
    DEFAULT_LABEL,
    MAX_NAME_CHARS,
    OMARCHY_ALTERNATIVES,
    OMARCHY_CONTRACT,
    OMARCHY_OPTIONAL,
    OMARCHY_REQUIRED,
    NoCloudSeed,
    SeedBuilder,
    SeedError,
    _BytesReader,
    _find_joliet,
    _iso9660_collisions,
    _iso9660_name,
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


def _contract_seed(**kw) -> NoCloudSeed:
    """A seed that declares the Omarchy contract instead of a file list."""
    kw.setdefault("extra_files", {"user_configuration.json": "{}",
                                  "user_credentials.json": '{"users": []}'})
    return NoCloudSeed(hostname="omarchy-vm", contract=OMARCHY_CONTRACT, **kw)


def _build_iso(path: Path, entries: dict[str, bytes], *, joliet: int = 3,
               label: str = "cidata") -> Path:
    """Write an ISO with pycdlib directly, bypassing this module.

    Used to produce images that are wrong in a way the builder would refuse to
    build, so verification can be tested against them.
    """
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=joliet, rock_ridge="1.09", vol_ident=label)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # pycdlib reads the source files during write(), so the staging
            # directory must still exist when write() runs.
            staging = Path(tmp)
            for name, data in entries.items():
                staged = staging / name
                staged.write_bytes(data)
                iso.add_file(str(staged), iso_path="/" + _iso9660_name(name),
                             rr_name=name, joliet_path="/" + name)
            iso.write(str(path))
    finally:
        iso.close()
    return path


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
        """The name is the installer's, not ours: the ISO reads
        ``user_encrypt_installation.txt`` (cidata-load:46). The old
        extension-less spelling was a filename no reader looks for, so the
        request was accepted and silently ignored at the guest."""
        assert "user_encrypt_installation.txt" not in NoCloudSeed().files()
        written = NoCloudSeed(encrypt=True).files()
        assert written["user_encrypt_installation.txt"] == b"true\n"
        assert "user_encrypt_installation" not in written

    def test_full_name_written_as_an_installer_document(self):
        """full_name must also land where the installer reads it."""
        files = NoCloudSeed(full_name="Omarchy VM").files()
        assert files["user_full_name.txt"] == b"Omarchy VM\n"
        assert "full_name: Omarchy VM" in files["user-data"].decode()

    def test_email_written_as_an_installer_document(self):
        """email used to be stored on the dataclass and never written
        anywhere: accepted, ignored, no account configured."""
        files = NoCloudSeed(email="someone@example.com").files()
        assert files["user_email_address.txt"] == b"someone@example.com\n"

    def test_no_installer_documents_when_fields_unset(self):
        files = NoCloudSeed().files()
        for name in ("user_full_name.txt", "user_email_address.txt",
                     "user_encrypt_installation.txt", "authorized_keys"):
            assert name not in files

    def test_extra_files_included(self):
        files = NoCloudSeed(extra_files={"a.txt": "hello"}).files()
        assert files["a.txt"] == b"hello"

    def test_extra_files_override_derived_document(self):
        """extra_files is applied last, so a caller can hand over the exact
        bytes the installer expects for a document the builder derives."""
        files = NoCloudSeed(email="a@b.c",
                            extra_files={"user_email_address.txt": "x@y.z"}).files()
        assert files["user_email_address.txt"] == b"x@y.z"

    def test_binary_extra_file_passed_through_as_bytes(self):
        """A binary payload must not be mangled by an encode() round trip."""
        blob = bytes(range(256))
        files = NoCloudSeed(extra_files={"blob.bin": blob}).files()
        assert files["blob.bin"] == blob

    @pytest.mark.parametrize("field", ["hostname", "username", "full_name",
                                       "email", "timezone"])
    def test_newline_in_a_field_is_refused(self, field):
        """A newline would let a caller inject arbitrary user-data keys, and
        the damage would only show up inside the guest."""
        with pytest.raises(SeedError, match="newline"):
            NoCloudSeed(**{field: "box\nusers: [root]"}).files()

    def test_non_string_field_refused(self):
        with pytest.raises(SeedError, match="must be a string"):
            NoCloudSeed(hostname=1234).files()


class TestDeferProvisioning:
    """The marker-based flow (AUTINSTALL-CONTRACT.md §1, §6b)."""

    def test_marker_written_zero_bytes(self):
        files = NoCloudSeed(defer_provisioning=True).files()
        assert "defer-provisioning" in files
        assert files["defer-provisioning"] == b""

    def test_marker_absent_by_default(self):
        assert "defer-provisioning" not in NoCloudSeed().files()

    def test_deferred_seed_needs_no_credentials(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_contract_seed(defer_provisioning=True,
                                          extra_files={"user_configuration.json": "{}"}), out)
        verify_seed(out, contract=OMARCHY_CONTRACT)
        assert b"user_credentials.json" not in out.read_bytes()

    def test_contract_accepts_marker_in_place_of_credentials(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_contract_seed(defer_provisioning=True), out)
        found = verify_seed(out, contract=OMARCHY_CONTRACT)
        assert "defer-provisioning" in found

    def test_contract_rejects_seed_with_neither(self, tmp_path):
        """The exact way #2 the contract lists: the seed looks fine and is
        ignored, because the required pair is incomplete."""
        seed = NoCloudSeed(hostname="box",
                           extra_files={"user_configuration.json": "{}"},
                           contract=OMARCHY_CONTRACT)
        with pytest.raises(SeedError, match="defer-provisioning"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_contract_rejects_seed_without_configuration(self, tmp_path):
        seed = NoCloudSeed(hostname="box",
                           extra_files={"user_credentials.json": '{"users": []}'},
                           contract=OMARCHY_CONTRACT)
        with pytest.raises(SeedError, match="user_configuration.json"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_deferred_seed_carrying_users_is_refused(self, tmp_path):
        """Deferred provisioning strips users from the credentials file
        (contract §3.4), so the install would finish with no account and no
        error. Refused up front instead."""
        seed = _contract_seed(
            defer_provisioning=True,
            extra_files={"user_configuration.json": "{}",
                         "user_credentials.json": '{"users": [{"username": "o"}]}'},
        )
        with pytest.raises(SeedError, match="strips them"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_deferred_seed_keeping_only_encryption_password_is_allowed(self, tmp_path):
        """The documented hybrid: no users, but the LUKS passphrase handed
        over for first-boot re-key (contract §6b)."""
        seed = _contract_seed(
            defer_provisioning=True,
            extra_files={"user_configuration.json": "{}",
                         "user_credentials.json": '{"encryption_password": "hunter2"}'},
        )
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(seed, out)
        verify_seed(out, contract=OMARCHY_CONTRACT)


class TestInstallerContract:
    def test_omarchy_contract_shape_matches_the_spec(self):
        assert OMARCHY_REQUIRED == ("user_configuration.json",
                                    "user_credentials.json")
        assert OMARCHY_OPTIONAL == ("user_full_name.txt", "user_email_address.txt",
                                    "user_encrypt_installation.txt", "authorized_keys",
                                    "tailscale_authkey", "defer-provisioning")
        assert OMARCHY_ALTERNATIVES == (("user_credentials.json",
                                          "defer-provisioning"),)
        # The marker substitutes for the credentials, so only the config file
        # is unconditionally required.
        assert OMARCHY_CONTRACT.hard_required() == ("user_configuration.json",)

    def test_contract_seed_verifies(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_contract_seed(), out)
        assert "user_configuration.json" in verify_seed(out, contract=OMARCHY_CONTRACT)

    def test_contract_requirements_replace_the_everything_default(self, tmp_path):
        """With a contract, only the contract is mandatory - optional inputs
        are still allowed to be absent."""
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_contract_seed(), out)
        assert b"tailscale_authkey" not in out.read_bytes()
        assert "user_configuration.json" in verify_seed(out, contract=OMARCHY_CONTRACT)

    def test_missing_reports_both_failures(self):
        problems = OMARCHY_CONTRACT.missing([])
        assert len(problems) == 2
        assert any("user_configuration.json" in p for p in problems)
        assert any("user_credentials.json" in p for p in problems)

    def test_missing_is_empty_when_satisfied(self):
        assert OMARCHY_CONTRACT.missing(
            ["user_configuration.json", "defer-provisioning"]) == []

    def test_build_seed_helper_accepts_a_contract(self, tmp_path):
        from vm_harness.cloudinit import build_seed

        out = tmp_path / "cidata.iso"
        build_seed(out, hostname="box", contract=OMARCHY_CONTRACT,
                   extra_files={"user_configuration.json": "{}",
                                "user_credentials.json": '{"users": []}'})
        assert "user_configuration.json" in verify_seed(out, contract=OMARCHY_CONTRACT)


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

        Built with pycdlib directly rather than by patching bytes out of a good
        image: verification reads the image's directory records, so the file
        has to genuinely not be there. Patching a name in the byte stream only
        rewrote the Rock Ridge alternate name while the Joliet entry kept
        saying "user-data" - which the old substring check never noticed.
        """
        broken = _build_iso(tmp_path / "nouserdata.iso",
                            {"meta-data": b"instance-id: nocloud\n"})
        with pytest.raises(SeedError, match="user-data"):
            verify_seed(broken)

    def test_seed_missing_meta_data_rejected(self, tmp_path):
        broken = _build_iso(tmp_path / "nometadata.iso",
                            {"user-data": b"#cloud-config\n"})
        with pytest.raises(SeedError, match="meta-data"):
            verify_seed(broken)

    def test_name_in_a_payload_does_not_count_as_the_file(self, tmp_path):
        """The old check grepped the whole image for the filename, so any
        string that happened to appear - in another file's contents, say -
        satisfied the requirement. Now only real directory entries count."""
        out = _build_iso(tmp_path / "liar.iso", {
            "meta-data": b"instance-id: nocloud\n",
            "user-data": b"#cloud-config\n# see user_credentials.json\n",
        })
        with pytest.raises(SeedError, match="required file missing"):
            verify_seed(out, ["user_credentials.json"])

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

    def test_auto_order_is_finite_and_unique(self):
        """The fallback list is a fixed set, never re-derived from a backend's
        own availability, so no backend can be retried forever."""
        order = SeedBuilder()._backend_order()
        assert order == list(dict.fromkeys(order))
        assert set(order) <= set(SeedBuilder.AUTO_ORDER)

    def test_imapi2_is_never_in_the_auto_order(self):
        """It raises by design, so a guaranteed failure must not sit in the
        fallback path - and on Windows it used to report itself available."""
        builder = SeedBuilder()
        assert builder._available("imapi2") is False
        assert "imapi2" not in builder._backend_order()

    def test_describe_reports_availability(self):
        described = SeedBuilder().describe()
        assert "pycdlib" in described
        assert described["pycdlib"] is True

    def test_describe_lists_every_backend_with_a_reason(self):
        """A backend that can never work is reported unavailable *with the
        reason*, not as available, and not omitted."""
        report = SeedBuilder().describe(detailed=True)
        assert set(report) == set(SeedBuilder.AUTO_ORDER)
        assert report["imapi2"]["available"] is False
        assert "Joliet" in report["imapi2"]["reason"]
        assert report["pycdlib"]["available"] is True
        assert report["pycdlib"]["reason"]
        for status in report.values():
            assert isinstance(status["available"], bool)
            assert status["reason"]

    def test_describe_reports_an_unknown_backend(self):
        report = SeedBuilder("nonsense").describe(detailed=True)
        assert report["nonsense"]["available"] is False
        assert "unknown backend" in report["nonsense"]["reason"]

    def test_unknown_backend_fails_loudly(self, tmp_path):
        with pytest.raises(SeedError):
            SeedBuilder("nonsense").build(_omarchy_seed(), tmp_path / "x.iso")

    def test_no_usable_backend_explains_each_one(self, tmp_path, monkeypatch):
        """With nothing installed the error must say why, not just that
        'no backend could build a valid NoCloud seed'."""
        builder = SeedBuilder()
        monkeypatch.setattr(type(builder), "_available", lambda self, n: False)
        with pytest.raises(SeedError, match="no usable NoCloud seed backend"):
            builder.build(_omarchy_seed(), tmp_path / "x.iso")
        assert not (tmp_path / "x.iso").exists()

    def test_imapi2_backend_refuses_with_guidance(self, tmp_path):
        """The Windows COM fallback must explain itself rather than emit an
        image NoCloud readers reject. Asked for by name on any platform: it is
        not a platform-gated code path, it simply refuses."""
        with pytest.raises(SeedError, match="pycdlib"):
            SeedBuilder("imapi2").build(_omarchy_seed(), tmp_path / "x.iso")

    def test_imapi2_refusal_leaves_no_image_behind(self, tmp_path):
        out = tmp_path / "x.iso"
        with pytest.raises(SeedError):
            SeedBuilder("imapi2").build(_omarchy_seed(), out)
        assert not out.exists()

    def test_failed_build_does_not_leave_a_partial_image(self, tmp_path, monkeypatch):
        """A half-written ISO is exactly the file that gets attached to a VM by
        mistake, so a failed attempt must clean up after itself."""
        builder = SeedBuilder()
        monkeypatch.setattr(builder, "_build_pycdlib",
                            lambda *a, **k: Path(a[2]).write_bytes(b"garbage" * 100))
        out = tmp_path / "cidata.iso"
        with pytest.raises(SeedError, match="no backend could build"):
            builder.build(_omarchy_seed(), out)
        assert not out.exists()


class TestConvenience:
    def test_build_seed_helper(self, tmp_path):
        out = tmp_path / "cidata.iso"
        from vm_harness.cloudinit import build_seed

        build_seed(out, hostname="box", extra_files={"a.json": "{}"})
        assert out.exists()
        verify_seed(out, ["a.json"])


class TestIso9660Names:
    @pytest.mark.parametrize("name,expected", [
        ("user_configuration.json", "USER_CON.JSO"),
        ("meta-data", "META_DAT"),
        ("user-data", "USER_DAT"),
        ("authorized_keys", "AUTHORIZ"),
        ("defer-provisioning", "DEFER_PR"),
        ("a.b.c", "A_B.C"),
    ])
    def test_known_manglings(self, name, expected):
        assert _iso9660_name(name) == expected

    def test_extensionless_name_gets_no_spurious_extension(self):
        """``rpartition`` hands the whole name back as the "extension" when
        there is no dot, so "user-data" used to become USER_DAT.USE - an 8.3
        name assembled from two halves of one filename."""
        assert _iso9660_name("user-data") == "USER_DAT"
        assert _iso9660_name("no-ext") == "NO_EXT"

    def test_non_ascii_is_not_folded_into_the_name(self):
        """str.isalnum() is true for 'e-acute' and for Arabic-Indic digits;
        neither is an ISO9660 d-character, and both would corrupt the name."""
        mangled = _iso9660_name("café.json")
        assert mangled == "CAF_.JSO"
        assert all(c.isascii() for c in mangled)

    def test_collisions_are_detected(self):
        assert _iso9660_collisions(
            ["user_configuration.json", "user_configs.json"]) == [
            ("user_configuration.json", "user_configs.json")]
        assert _iso9660_collisions(["meta-data", "user-data", "a.txt"]) == []

    def test_omarchy_document_set_has_no_collisions(self):
        """The contract's own names must survive the mangler untouched."""
        assert _iso9660_collisions(list(OMARCHY_OPTIONAL) + list(OMARCHY_REQUIRED)) == []


class TestPreflight:
    """Failures that must be caught before a backend burns time, and that
    would otherwise be silent."""

    def test_empty_seed_refused(self, tmp_path):
        """NoCloudSeed always writes the two cloud-init documents, so an empty
        seed can only come from a bypassed builder - and pycdlib will happily
        produce a structurally perfect, completely unusable ISO for one."""
        with pytest.raises(SeedError, match="empty seed"):
            SeedBuilder()._preflight({}, DEFAULT_LABEL)

    def test_empty_iso_would_not_verify(self, tmp_path):
        """What the builder refuses to produce, checked at the other end: even
        with a correct label and a Joliet SVD, an image with no files in it
        cannot be a seed."""
        empty = _build_iso(tmp_path / "empty.iso", {})
        with pytest.raises(SeedError, match="meta-data"):
            verify_seed(empty)

    def test_8_3_collision_refused(self, tmp_path):
        """The silent case: pycdlib emits *both* files, with both payloads and
        both Joliet names intact, and two directory entries sharing one 8.3
        name - an invalid directory no reader can disambiguate. Refused."""
        seed = NoCloudSeed(hostname="box", extra_files={
            "user_configuration.json": '{"a": 1}',
            "user_configs.json": '{"b": 2}',
        })
        out = tmp_path / "cidata.iso"
        with pytest.raises(SeedError, match="8.3 name collision"):
            SeedBuilder().build(seed, out)
        assert not out.exists()

    def test_collision_message_names_both_files(self, tmp_path):
        seed = NoCloudSeed(hostname="box", extra_files={
            "user_configuration.json": "{}", "user_configs.json": "{}"})
        with pytest.raises(SeedError) as excinfo:
            SeedBuilder().build(seed, tmp_path / "cidata.iso")
        message = str(excinfo.value)
        assert "user_configuration.json" in message
        assert "user_configs.json" in message
        assert "USER_CON.JSO" in message

    def test_case_only_difference_refused(self, tmp_path):
        seed = NoCloudSeed(hostname="box", extra_files={"Seeds.txt": "a", "seeds.txt": "b"})
        with pytest.raises(SeedError, match="differ only in case"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    @pytest.mark.parametrize("name", ["a/b.txt", "..\\evil.txt", ".", ".."])
    def test_path_like_name_refused(self, tmp_path, name):
        seed = NoCloudSeed(hostname="box", extra_files={name: "x"})
        with pytest.raises(SeedError, match="single path component"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_control_character_in_name_refused(self, tmp_path):
        seed = NoCloudSeed(hostname="box", extra_files={"bad\x01name": "x"})
        with pytest.raises(SeedError, match="control character"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_over_long_name_refused(self, tmp_path):
        name = "n" * (MAX_NAME_CHARS + 1) + ".json"
        seed = NoCloudSeed(hostname="box", extra_files={name: "{}"})
        with pytest.raises(SeedError, match="over the"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_non_cidata_label_refused(self, tmp_path):
        """A NoCloud consumer mounts by label, so any other label produces an
        image nothing will ever read. Caught with the reason, not at verify."""
        seed = NoCloudSeed(hostname="box", label="mydata")
        with pytest.raises(SeedError, match="volume label"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_uppercase_cidata_label_accepted(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(NoCloudSeed(hostname="box", label="CIDATA"), out)
        verify_seed(out)

    def test_mixed_case_cidata_label_accepted(self, tmp_path):
        """Verification compares the label case-insensitively, so preflight
        does too - the two must not disagree about what is acceptable."""
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(NoCloudSeed(hostname="box", label="Cidata"), out)
        verify_seed(out)

    def test_non_string_label_refused(self, tmp_path):
        seed = NoCloudSeed(hostname="box", label=None)
        with pytest.raises(SeedError, match="non-empty string"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_bad_payload_type_refused(self, tmp_path):
        seed = NoCloudSeed(hostname="box", extra_files={"n.txt": 1234})
        with pytest.raises(SeedError, match="must be str or bytes"):
            SeedBuilder().build(seed, tmp_path / "cidata.iso")

    def test_bytearray_payload_accepted(self, tmp_path):
        out = tmp_path / "cidata.iso"
        seed = NoCloudSeed(hostname="box", extra_files={"b.bin": bytearray(b"\x00\xff")})
        SeedBuilder().build(seed, out)
        assert b"\x00\xff" in out.read_bytes()


class TestPayloads:
    def test_binary_payload_survives_the_round_trip(self, tmp_path):
        """Binary installers hand over signed images and locale blobs; a text
        round trip through the seed would corrupt them."""
        blob = bytes(range(256)) * 16
        out = tmp_path / "cidata.iso"
        seed = NoCloudSeed(hostname="box", extra_files={"payload.bin": blob})
        SeedBuilder().build(seed, out)
        assert verify_seed(out, ["payload.bin"])
        assert blob in out.read_bytes()

    def test_utf16_text_payload_survives(self, tmp_path):
        """Encoded text that is not valid UTF-8 - the caller's bytes, not ours."""
        payload = "héllo wörld".encode("utf-16")
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(NoCloudSeed(hostname="box",
                                        extra_files={"note.txt": payload}), out)
        assert payload in out.read_bytes()

    def test_zero_length_payload_written(self, tmp_path):
        """The defer marker is zero bytes; pycdlib must still emit an entry for
        it, because the loader tests the path, not the size."""
        out = tmp_path / "cidata.iso"
        seed = _contract_seed(defer_provisioning=True)
        SeedBuilder().build(seed, out)
        assert "defer-provisioning" in verify_seed(out, contract=OMARCHY_CONTRACT)

    def test_json_documents_round_trip_and_parse(self, tmp_path):
        config = {"hostname": "omarchy-vm", "kernels": ["linux-omarchy"],
                  "omarchy_install": {"mode": "full_disk", "defer_provisioning": False}}
        creds = {"root_enc_password": "$yescrypt$j9T$x",
                 "users": [{"username": "omarchy", "enc_password": "$yescrypt$j9T$x",
                            "sudo": True, "groups": ["wheel"]}]}
        out = tmp_path / "cidata.iso"
        seed = _contract_seed(extra_files={"user_configuration.json": json.dumps(config),
                                           "user_credentials.json": json.dumps(creds)})
        SeedBuilder().build(seed, out)
        blob = out.read_bytes()
        assert json.dumps(config).encode() in blob
        assert json.dumps(creds).encode() in blob


class TestBytesReader:
    def test_reads_and_seeks(self):
        reader = _BytesReader(b"abcdef")
        assert reader.read(3) == b"abc"
        assert reader.tell() == 3
        assert reader.read() == b"def"
        assert reader.read() == b""
        assert reader.seek(0) == 0
        assert reader.read(1) == b"a"
        assert reader.seek(-2, os.SEEK_END) == 4
        assert reader.read() == b"ef"

    def test_satisfies_pycdlib_binary_file_check(self):
        """pycdlib rejects anything that does not look like a binary file
        object, which is why this is an io.RawIOBase."""
        from pycdlib import utils

        assert utils.file_object_supports_binary(_BytesReader(b""))

    def test_readinto_fills_a_buffer(self):
        reader = _BytesReader(b"abcdef")
        buf = bytearray(3)
        assert reader.readinto(buf) == 3
        assert bytes(buf) == b"abc"


class TestDescriptorScanning:
    """The 24-sector window that makes ``_find_joliet`` sufficient."""

    def test_pycdlib_writes_the_joliet_svd_at_sector_17(self, tmp_path):
        """Documented expectation behind SVD_SEARCH_END: one supplementary
        descriptor, immediately after the PVD, terminator at 18."""
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(NoCloudSeed(hostname="box"), out)
        data = out.read_bytes()
        assert data[16 * 2048] == 1
        assert data[17 * 2048] == 2 and data[17 * 2048 + 88:17 * 2048 + 91] in (
            b"%/@", b"%/C", b"%/E")
        assert data[18 * 2048] == 255  # volume descriptor set terminator
        assert _find_joliet(data) == 17 * 2048

    def test_joliet_is_found_past_a_rock_ridge_svd(self, tmp_path):
        """An SVD that is not Joliet must not be mistaken for one: the old code
        stopped at the first supplementary descriptor and would have reported
        this image as having no Joliet at all.

        Built by relocating the Joliet SVD one sector later and dropping a
        Rock Ridge SVD in front of it, which is the layout a writer that emits
        both separately would produce.
        """
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(NoCloudSeed(hostname="box"), out)
        data = bytearray(out.read_bytes())
        joliet_sector = bytes(data[17 * 2048:18 * 2048])
        rr = bytearray(joliet_sector)
        rr[88:91] = b"RX\x00"                      # not a Joliet escape sequence
        data[17 * 2048:18 * 2048] = rr             # Rock Ridge SVD at 17
        data[18 * 2048:19 * 2048] = joliet_sector  # Joliet SVD at 18
        assert _find_joliet(bytes(data)) == 18 * 2048

    def test_absent_joliet_reports_none(self, tmp_path):
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(NoCloudSeed(hostname="box"), out)
        data = bytearray(out.read_bytes())
        data[17 * 2048] = 255  # the Joliet SVD becomes the terminator
        assert _find_joliet(bytes(data)) is None


class TestDuplicateIsoNames:
    def test_image_with_two_files_under_one_8_3_name_is_rejected(self, tmp_path):
        """Verification has to catch the ambiguous image even though no backend
        this module ships would build one: the checks must not depend on the
        builder's cooperation."""
        out = tmp_path / "ambiguous.iso"
        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="cidata")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                staging = Path(tmp)
                for name in ("user_configuration.json", "user_configs.json"):
                    staged = staging / name
                    staged.write_bytes(b"{}")
                    # Both are given the *same* ISO9660 identifier on purpose.
                    iso.add_file(str(staged), iso_path="/USER_CON.JSO",
                                 rr_name=name, joliet_path="/" + name)
                iso.write(str(out))
        finally:
            iso.close()
        with pytest.raises(SeedError, match="share the ISO9660 name"):
            verify_seed(out)


def _skip_without_powershell() -> None:
    if os.name != "nt":
        pytest.skip("Mount-DiskImage is Windows-only")


class TestIndependentParse:
    """Hand the finished image to Windows and see what it finds.

    Everything else in this file compares a build against this module's own
    parser. Mounting proves a second, unrelated implementation agrees on the
    filenames - the property that actually matters, since the guest is a third
    implementation.
    """

    @staticmethod
    def _mount(image: Path) -> dict:
        """Mount, list and read the image, then always dismount.

        Returns {"label": str, "names": {name: length}, "contents": {name: bytes}}.
        Skips - rather than fails - when the host will not let it: mounting a
        disk image needs privileges and a filesystem driver that a CI box may
        not have.

        The image is copied out of pytest's ``tmp_path`` first. Windows on this
        host attaches an image from anywhere under ``.../Temp/pytest-of-*``
        but never materialises a volume for it - the optical image shows up in
        Get-DiskImage with no drive letter and no label, so the mount silently
        yields nothing. Copying to a neutral directory sidesteps that; if the
        mount still does not happen the test skips.

        Windows also declines to hand a freshly mounted optical image a drive
        letter immediately, and refuses outright while the optical drive is
        still settling after the previous image was ejected, so the mount is
        retried a few times before giving up.
        """
        _skip_without_powershell()
        staging = Path(tempfile.mkdtemp(prefix="vh-seed-mount-"))
        try:
            mounted = staging / image.name
            shutil.copy2(image, mounted)
            script = staging / "mount.ps1"
            script.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                "$image = '{image}'\n"
                "$letter = $null\n"
                "$label = $null\n"
                "$lastError = ''\n"
                "for ($attempt = 1; $attempt -le 3 -and -not $letter; $attempt++) {{\n"
                "  try {{\n"
                "    $img = Mount-DiskImage -ImagePath $image -PassThru -ErrorAction Stop\n"
                "    for ($i = 0; $i -lt 12 -and -not $letter; $i++) {{\n"
                "      $vol = $img | Get-Volume -ErrorAction SilentlyContinue\n"
                "      if ($vol -and $vol.DriveLetter) {{ $letter = $vol.DriveLetter; $label = $vol.FileSystemLabel }}\n"
                "      if (-not $letter) {{ Start-Sleep -Milliseconds 250 }}\n"
                "    }}\n"
                "  }} catch {{ $lastError = $_.Exception.Message }}\n"
                "  if ($letter) {{ break }}\n"
                "  try {{ Dismount-DiskImage -ImagePath $image -ErrorAction SilentlyContinue | Out-Null }} catch {{}}\n"
                "  Start-Sleep -Seconds 4\n"
                "}}\n"
                "if (-not $letter) {{\n"
                "  Write-Output ('MOUNT-FAILED ' + $lastError)\n"
                "}} else {{\n"
                "  $root = $letter + ':\\'\n"
                "  Write-Output ('LABEL|' + $label)\n"
                "  Get-ChildItem -LiteralPath $root -Force | ForEach-Object {{\n"
                "    Write-Output ('NAME|' + $_.Name + '|' + $_.Length)\n"
                "  }}\n"
                "  Get-ChildItem -LiteralPath $root -File -Force | ForEach-Object {{\n"
                "    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)\n"
                "    Write-Output ('HEX|' + $_.Name + '|' + [System.BitConverter]::ToString($bytes))\n"
                "  }}\n"
                "  try {{ Dismount-DiskImage -ImagePath $image -ErrorAction SilentlyContinue | Out-Null }} catch {{}}\n"
                "  Start-Sleep -Seconds 2\n"
                "}}\n".format(image=mounted),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-File", str(script)],
                capture_output=True, text=True, timeout=300,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if result.returncode != 0:
            pytest.skip(f"PowerShell could not run: {result.stderr[-400:]}")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        failures = [line for line in lines if line.startswith("MOUNT-FAILED")]
        if failures:
            pytest.skip(f"Mount-DiskImage unavailable here: {failures[0]}")

        label, names, contents = None, {}, {}
        for line in lines:
            if line.startswith("LABEL|"):
                label = line[len("LABEL|"):]
            elif line.startswith("NAME|"):
                _, name, length = line.split("|", 2)
                names[name] = int(length)
            elif line.startswith("HEX|"):
                _, name, hexed = line.split("|", 2)
                contents[name] = bytes.fromhex(hexed.replace("-", ""))
        return {"label": label, "names": names, "contents": contents}

    def test_windows_sees_the_filenames_we_wrote(self, tmp_path):
        blob = bytes(range(256)) * 8
        seed = _contract_seed(encrypt=True, authorized_keys=["ssh-ed25519 AAAA test"],
                              extra_files={
                                  "user_configuration.json": '{"hostname": "omarchy-vm"}',
                                  "user_credentials.json": '{"users": []}',
                                  "tailscale_authkey": "tskey-auth-abc123",
                                  "payload.bin": blob,
                              })
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(seed, out)

        seen = self._mount(out)
        assert seen["label"] == "cidata"
        found = seen["names"]
        for name in ("meta-data", "user-data", "user_configuration.json",
                     "user_credentials.json", "tailscale_authkey",
                     "authorized_keys", "user_encrypt_installation.txt",
                     "payload.bin"):
            assert name in found, f"{name} missing from the mounted image: {sorted(found)}"
        # Lengths and contents as Windows read them, not as we wrote them.
        assert found["tailscale_authkey"] == len("tskey-auth-abc123")
        assert found["user_encrypt_installation.txt"] == len(b"true\n")
        assert seen["contents"]["user_configuration.json"] == b'{"hostname": "omarchy-vm"}'
        assert seen["contents"]["payload.bin"] == blob
        assert seen["contents"]["user-data"].startswith(b"#cloud-config")

    def test_windows_sees_the_defer_marker(self, tmp_path):
        """A zero-byte file must appear at its full length in the name."""
        out = tmp_path / "cidata.iso"
        SeedBuilder().build(_contract_seed(defer_provisioning=True), out)
        seen = self._mount(out)
        assert seen["names"]["defer-provisioning"] == 0
