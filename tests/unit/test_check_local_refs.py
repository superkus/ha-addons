"""The local-setup reference guard: generic rules, the private denylist, and its three scan modes.

Every sample that the guard must *reject* is assembled at runtime from fragments, so this file
never contains a literal private address, path, or personal e-mail and passes the guard itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tools.check_local_refs import (
    ALLOW_MARKER,
    DENYLIST_ENV,
    DENYLIST_FILE,
    load_denylist,
    main,
    scan_text,
)


def rules_hit(text: str, denylist: tuple[str, ...] = ()) -> list[str]:
    return [finding.rule for finding in scan_text("sample.txt", text, denylist)]


DOT = "."
AT = "@"


# --------------------------------------------------------------------------------------------------
# generic rules: what is rejected
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sample", "rule"),
    [
        ("host: " + DOT.join(["192", "168", "1", "9"]), "private-ipv4"),
        ("url = 'http://" + DOT.join(["10", "1", "2", "3"]) + ":8080/'", "private-ipv4"),
        ("gw " + DOT.join(["172", "16", "0", "1"]), "private-ipv4"),
        ("gw " + DOT.join(["172", "31", "255", "254"]), "private-ipv4"),
        ("vpn " + DOT.join(["100", "64", "0", "1"]), "cgnat-or-link-local-ipv4"),
        ("apipa " + DOT.join(["169", "254", "10", "20"]), "cgnat-or-link-local-ipv4"),
        ("mac " + ":".join(["ec", "a8", "6b", "fb", "ab", "d2"]), "mac-address"),
        ("see /" + "Users/" + "alice/projects", "home-directory"),
        ("cd /" + "home/" + "bob/src", "home-directory"),
        ("C:\\" + "Users\\" + "carol\\repo", "home-directory"),
        ("rsync /" + "Volumes/" + "nas-share/x .", "mounted-volume"),
        ("mount at /private/" + "nfs/workspace", "mounted-volume"),
        ("ping nas" + DOT + "local", "lan-hostname"),
        ("ssh router" + DOT + "lan", "lan-hostname"),
        ("api" + DOT + "corp" + DOT + "internal:443", "lan-hostname"),
        ("hass" + DOT + "home" + DOT + "arpa", "lan-hostname"),
        ("mail me: jane" + AT + "personal-domain" + DOT + "es", "email-address"),
        ("author " + "ops" + AT + "company" + DOT + "com", "email-address"),
    ],
)
def test_generic_rules_reject_local_setup_references(sample: str, rule: str) -> None:
    assert rule in rules_hit(sample)


# --------------------------------------------------------------------------------------------------
# generic rules: what is accepted
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sample",
    [
        "host: 192.0.2.9  # RFC 5737 TEST-NET-1",
        "host: 198.51.100.7 or 203.0.113.250",
        "loopback 127.0.0.1 and 0.0.0.0",
        "public 8.8.8.8",
        "not an address: 1.10.0.0.5, version 3.10.0.1.2",
        "172.15.0.1 and 172.32.0.1 are outside 172.16/12",
        "doc mac 00:00:5e:00:53:01",
        "timestamp 12:34:56:78 is not a MAC",
        "home = Path.home() / '.cache'",
        "runner path /home/runner/work and ~/projects",
        "/Users/<you>/projects and /Volumes/<share>",
        "/private/tmp/remote-energy-meter and /private/var/folders",
        "device idrac.example, broker.internal.example, ha.example.com",
        "contact noreply@example.invalid or a@b.example or c@example.com",
        "icon@2x.png and logo@2x.png are file names",
        "12345+superkus@users.noreply.github.com",
    ],
)
def test_generic_rules_accept_documentation_values(sample: str) -> None:
    assert rules_hit(sample) == []


def test_the_allow_marker_opts_one_line_out_explicitly() -> None:
    private = DOT.join(["192", "168", "0", "1"])

    assert rules_hit(f"{private}  # {ALLOW_MARKER}") == []
    assert rules_hit(f"{private}\n{private}  # {ALLOW_MARKER}") == ["private-ipv4"]


def test_findings_name_the_line_and_never_drop_context() -> None:
    text = "ok\n" + "x " + DOT.join(["10", "0", "0", "1"]) + "\nok\n"

    (finding,) = scan_text("cfg.yaml", text)

    assert (finding.location, finding.line, finding.rule) == ("cfg.yaml", 2, "private-ipv4")
    assert "cfg.yaml:2: private-ipv4" in finding.render()


# --------------------------------------------------------------------------------------------------
# the private denylist
# --------------------------------------------------------------------------------------------------


def test_denylist_terms_match_case_insensitively_as_literals() -> None:
    hits = rules_hit("Deployed on MyHost-01 via (staging)", ("myhost-01", "(staging)"))

    assert hits == ["private-denylist", "private-denylist"]


def test_the_denylist_is_read_from_the_repository_root_and_skips_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The environment override takes precedence, so a developer's own setting must not leak in.
    monkeypatch.delenv(DENYLIST_ENV, raising=False)
    (tmp_path / DENYLIST_FILE).write_text("# personal terms\n\nalpha-host\n  beta.example.org  \n")

    assert load_denylist(tmp_path) == ("alpha-host", "beta.example.org")


def test_the_denylist_location_can_come_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = tmp_path / "terms.txt"
    elsewhere.write_text("gamma\n")
    monkeypatch.setenv(DENYLIST_ENV, str(elsewhere))

    assert load_denylist(tmp_path / "repo") == ("gamma",)


def test_no_denylist_file_means_generic_rules_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DENYLIST_ENV, raising=False)

    assert load_denylist(tmp_path) == ()


# --------------------------------------------------------------------------------------------------
# the three scan modes, against a real throwaway repository
# --------------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(DENYLIST_ENV, raising=False)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.hooksPath", "/dev/null")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_tracked_mode_scans_working_tree_files(repo: Path) -> None:
    (repo / "clean.md").write_text("host 192.0.2.1\n")
    _git(repo, "add", "clean.md")
    assert main([]) == 0

    (repo / "clean.md").write_text("host " + DOT.join(["192", "168", "7", "7"]) + "\n")
    assert main([]) == 1


def test_staged_mode_scans_the_index_not_the_working_tree(repo: Path) -> None:
    leak = repo / "notes.txt"
    leak.write_text("nas at /" + "Volumes/" + "share\n")
    _git(repo, "add", "notes.txt")
    leak.write_text("fixed in the working tree, but not staged\n")

    assert main(["--staged"]) == 1


def test_history_mode_finds_a_reference_that_was_already_removed(repo: Path) -> None:
    file = repo / "config.yaml"
    file.write_text("host: " + DOT.join(["10", "9", "8", "7"]) + "\n")
    _git(repo, "add", "config.yaml")
    _git(repo, "commit", "-q", "-m", "add")
    file.write_text("host: 192.0.2.7\n")
    _git(repo, "commit", "-q", "-am", "fix")

    assert main([]) == 0, "the current tree is clean"
    assert main(["--history"]) == 1, "but publishing would still expose the old version"


def test_history_mode_scans_what_is_published_not_local_tool_refs(repo: Path) -> None:
    (repo / "readme.md").write_text("clean\n")
    _git(repo, "add", "readme.md")
    _git(repo, "commit", "-q", "-m", "init")
    # A local tool's cache ref (never pushed) pointing at a blob with a private address.
    leak = repo / "scan-report.txt"
    leak.write_text("scanned " + DOT.join(["192", "168", "3", "3"]) + "\n")
    blob = subprocess.run(["git", "hash-object", "-w", str(leak)], cwd=repo, check=True, capture_output=True, text=True)
    _git(repo, "update-ref", "refs/tool-cache/report", blob.stdout.strip())
    leak.unlink()

    assert main(["--history"]) == 0, "refs outside branches and tags are not published"

    _git(repo, "tag", "-a", "v1", "-m", "release", blob.stdout.strip())
    assert main(["--history"]) == 1, "a tag is published, so what it reaches is scanned"


def test_the_private_denylist_applies_in_every_mode(repo: Path) -> None:
    (repo / DENYLIST_FILE).write_text("secret-hostname\n")
    (repo / "readme.md").write_text("runs on Secret-Hostname\n")
    _git(repo, "add", "readme.md")

    assert main([]) == 1
    assert main(["--staged"]) == 1


def test_binary_files_are_skipped(repo: Path) -> None:
    (repo / "image.png").write_bytes(b"\x89PNG\r\n" + DOT.join(["192", "168", "1", "1"]).encode())
    _git(repo, "add", "image.png")

    assert main([]) == 0
