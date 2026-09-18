"""Shared repo URL and commit validation: refusal rules for the sandboxes."""

from __future__ import annotations

import pytest

from core import urlcheck


class TestSafeRepoUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/repo.git",
            "http://example.com/repo.git",
            "git@github.example:org/repo.git",
            "ssh://git@host/repo.git",
            "git://host/repo.git",
        ],
    )
    def test_allowed_schemes(self, url: str) -> None:
        assert urlcheck.is_safe_repo_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "ftp://example.com/repo.git",
            "file:///etc/passwd",
            "C:\\repos\\thing",
            "repo.git",
        ],
    )
    def test_refused_urls(self, url: str) -> None:
        assert urlcheck.is_safe_repo_url(url) is False

    def test_file_url_blocked_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("MANTRA_ALLOW_FILE_URL", raising=False)
        assert urlcheck.is_safe_repo_url("file:///tmp/repo") is False

    def test_file_url_allowed_with_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("MANTRA_ALLOW_FILE_URL", "1")
        assert urlcheck.is_safe_repo_url("file:///tmp/repo") is True

    def test_length_cap(self) -> None:
        assert urlcheck.is_safe_repo_url("https://x/" + "a" * 2048) is False
        assert urlcheck.is_safe_repo_url("https://x/" + "a" * 2000) is True

    @pytest.mark.parametrize(
        "bad_char",
        ["\n", "\r", "\x00"],
    )
    def test_control_characters_refused(self, bad_char: str) -> None:
        assert urlcheck.is_safe_repo_url(f"https://x/repo{bad_char}git") is False


class TestSafeCommit:
    @pytest.mark.parametrize(
        "commit",
        ["abc123", "HEAD", "HEAD~1", "v1.2.3", "9fceb02d0ae598e95dc970b24e0dcf2d029cc6a4"],
    )
    def test_allowed_refs(self, commit: str) -> None:
        assert urlcheck.is_safe_commit(commit) is True

    @pytest.mark.parametrize(
        "commit",
        [
            "",
            "   ",
            "a;b",
            "a&b",
            "a|b",
            "a`b",
            "a$b",
            "a(b)",
            "a<b",
            'a"b',
            "a'b",
        ],
    )
    def test_shell_metacharacters_refused(self, commit: str) -> None:
        assert urlcheck.is_safe_commit(commit) is False

    def test_length_cap(self) -> None:
        assert urlcheck.is_safe_commit("a" * 257) is False
        assert urlcheck.is_safe_commit("a" * 256) is True

    @pytest.mark.parametrize("bad_char", ["\n", "\r", "\x00"])
    def test_control_characters_refused(self, bad_char: str) -> None:
        assert urlcheck.is_safe_commit(f"abc{bad_char}123") is False


def test_sandbox_seams_use_shared_rules() -> None:
    """The sandbox classes keep their staticmethod seams over the shared code."""
    from core.container import DockerSandbox
    from core.sandbox import LocalSandbox

    assert LocalSandbox._is_safe_repo_url is urlcheck.is_safe_repo_url
    assert LocalSandbox._is_safe_commit is urlcheck.is_safe_commit
    assert DockerSandbox._is_safe_repo_url is urlcheck.is_safe_repo_url
    assert DockerSandbox._is_safe_commit is urlcheck.is_safe_commit
