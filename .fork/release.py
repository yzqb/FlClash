#!/usr/bin/env python3
"""Stable upstream snapshot -> arm64 APK -> verified draft -> public release."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

UPSTREAM = "chen08209/FlClash"
FORK = "yzqb/FlClash"
ANDROID = "{http://schemas.android.com/apk/res/android}"
IMPELLER = "io.flutter.embedding.android.EnableImpeller"
STABLE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def run(*args, cwd=None):
    result = subprocess.run([str(a) for a in args], cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def api(path, missing=False):
    result = subprocess.run(["gh", "api", path], text=True, capture_output=True)
    if result.returncode:
        if missing and "HTTP 404" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def one_replace(text, before, after):
    if after in text and before not in text:
        return text
    if text.count(before) != 1:
        raise RuntimeError(f"Upstream changed the expected source anchor: {before}")
    return text.replace(before, after, 1)


def patch_sources(repo):
    repo = Path(repo)
    path = repo / "lib/common/constant.dart"
    text = path.read_text()
    edits = {path: one_replace(text, "const repository = 'chen08209/FlClash';",
                               "const repository = 'yzqb/FlClash';")}
    path = repo / "lib/common/request.dart"
    text = path.read_text()
    edits[path] = one_replace(text, "remoteVersion.replaceAll('v', ''), version",
                               "remoteVersion.replaceAll('v', '').split('-').first, version")
    path = repo / "android/app/src/main/AndroidManifest.xml"
    text = path.read_text()
    app = ET.fromstring(text).find("application")
    if app is None:
        raise RuntimeError("Missing application element")
    entries = [e for e in app.findall("meta-data") if e.get(ANDROID + "name") == IMPELLER]
    if not entries:
        match = re.search(r"^[ \t]*<activity\b", text, re.M)
        if not match:
            raise RuntimeError("Missing activity insertion anchor")
        text = text[:match.start()] + (
            '        <meta-data android:name="' + IMPELLER + '" android:value="false" />\n'
        ) + text[match.start():]
    elif len(entries) != 1 or entries[0].get(ANDROID + "value") != "false":
        raise RuntimeError("Unexpected existing Impeller override; review upstream changes")
    edits[path] = text
    for path, contents in edits.items():
        path.write_text(contents)
    check_sources(repo)


def check_sources(repo):
    repo = Path(repo)
    assert "const repository = 'yzqb/FlClash';" in (repo / "lib/common/constant.dart").read_text()
    request = (repo / "lib/common/request.dart").read_text()
    assert "remoteVersion.replaceAll('v', '').split('-').first, version" in request
    assert "https://api.github.com/repos/$repository/releases/latest" in request
    assert "https://github.com/$repository/releases/latest" in request
    update = (repo / "lib/common/app_update.dart").read_text()
    assert "const appReleaseLine = ReleaseLine.dev;" in update
    assert "'/$repository/releases/download/$tag/$name'" in update
    assert "Abi.androidArm64 => apk(ReleaseArch.arm64)" in update
    assert "appEnv != 'stable'" in (repo / "lib/state.dart").read_text()
    app = ET.parse(repo / "android/app/src/main/AndroidManifest.xml").getroot().find("application")
    entries = [e for e in app.findall("meta-data") if e.get(ANDROID + "name") == IMPELLER]
    assert len(entries) == 1 and entries[0].get(ANDROID + "value") == "false"


def stable_release(tag):
    if tag:
        if not STABLE.fullmatch(tag):
            raise RuntimeError("upstream_tag must be vX.Y.Z")
        release = api(f"repos/{UPSTREAM}/releases/tags/{tag}")
    else:
        release = api(f"repos/{UPSTREAM}/releases/latest", missing=True)
        if not release or release["draft"] or release["prerelease"] or not STABLE.fullmatch(release["tag_name"]):
            candidates = []
            for page in range(1, 11):
                releases = api(f"repos/{UPSTREAM}/releases?per_page=100&page={page}")
                candidates.extend(r for r in releases if not r["draft"] and not r["prerelease"]
                                  and STABLE.fullmatch(r["tag_name"]))
                if len(releases) < 100:
                    break
            else:
                raise RuntimeError("Too many upstream releases; review pagination before selecting a baseline")
            if not candidates:
                raise RuntimeError("No stable vX.Y.Z release exists")
            release = max(candidates, key=lambda r: tuple(map(int, STABLE.fullmatch(r["tag_name"]).groups())))
    if release["draft"] or release["prerelease"] or not STABLE.fullmatch(release["tag_name"]):
        raise RuntimeError("The requested upstream release is not stable")
    return release


def toolchains(repo, sha):
    workflow = run("git", "show", f"{sha}:.github/workflows/build.yaml", cwd=repo)
    versions = {}
    for output, name in [("flutter", "FLUTTER_VERSION"), ("go", "GO_VERSION")]:
        found = re.search(rf"^  {name}: ['\"]([0-9]+\.[0-9]+\.[0-9]+)['\"]\s*$", workflow, re.M)
        if not found:
            raise RuntimeError(f"Review upstream toolchain: {name}")
        versions[output] = found[1]
    libraries = run("git", "show", f"{sha}:android/gradle/libs.versions.toml", cwd=repo)
    for output, name, pattern in [("sdk", "compileSdk", r"\d+"),
                                  ("ndk", "ndkVersion", r"[0-9.]+")]:
        found = re.search(rf'^{name} = "({pattern})"\s*$', libraries, re.M)
        if not found:
            raise RuntimeError(f"Review upstream Android toolchain: {name}")
        versions[output] = found[1]
    rust = run("git", "show", f"{sha}:plugins/rust_api/rust/rust-toolchain.toml", cwd=repo)
    found = re.search(r'^channel = "([0-9]+\.[0-9]+\.[0-9]+)"$', rust, re.M)
    if not found:
        raise RuntimeError("Upstream Rust must remain pinned")
    versions["rust"] = found[1]
    return versions


def context_path():
    return Path(os.environ["RUNNER_TEMP"]) / "flclash-release-context.json"


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        for key, value in values.items():
            if "\n" in str(value) or "\r" in str(value):
                raise RuntimeError("Multiline workflow output")
            handle.write(f"{key}={value}\n")


def prepare():
    if os.environ.get("GITHUB_REPOSITORY") != FORK:
        raise RuntimeError("This workflow is only for yzqb/FlClash")
    control = Path.cwd()
    release = stable_release(os.environ.get("REQUESTED_TAG", "").strip())
    upstream_tag = release["tag_name"]
    fork_tag = f"{upstream_tag}-yzqb.1"
    existing_release = api(f"repos/{FORK}/releases/tags/{fork_tag}", missing=True)
    if existing_release and not existing_release["draft"]:
        if existing_release["prerelease"]:
            raise RuntimeError("Existing fork release is incorrectly marked as a prerelease")
        if (existing_release["body"] or "") != (release["body"] or ""):
            raise RuntimeError("Published body differs from upstream; do not silently rewrite a release")
        names = {a["name"] for a in existing_release["assets"]}
        version = fork_tag[1:]
        if not {f"FlClash-dev-{version}-android-arm64.apk", f"FlClash-dev-{version}-SHA256SUMS"} <= names:
            raise RuntimeError("Published release is incomplete")
        print(f"Already published: {fork_tag}")
        output(skip="true")
        return
    run("git", "fetch", "--no-tags", f"https://github.com/{UPSTREAM}.git",
        f"refs/tags/{upstream_tag}:refs/fork-upstream/{upstream_tag}", cwd=control)
    upstream_sha = run("git", "rev-parse", f"refs/fork-upstream/{upstream_tag}^{{commit}}", cwd=control)
    versions = toolchains(control, upstream_sha)
    source = Path(os.environ["RUNNER_TEMP"]) / "flclash-source"
    if source.exists():
        raise RuntimeError("Unexpected existing build worktree")
    remote_tag = api(f"repos/{FORK}/git/ref/tags/{fork_tag}", missing=True)
    branch = os.environ["RELEASE_BRANCH"]
    control_sha = run("git", "rev-parse", "HEAD", cwd=control)
    if remote_tag:
        run("git", "fetch", "--no-tags", "origin",
            f"refs/tags/{fork_tag}:refs/fork-build/{fork_tag}", cwd=control)
        custom_sha = run("git", "rev-parse", f"refs/fork-build/{fork_tag}^{{commit}}", cwd=control)
        run("git", "worktree", "add", "--detach", source, custom_sha, cwd=control)
        record = json.loads((source / ".fork/build.json").read_text())
        if record["upstream_sha"] != upstream_sha or record["upstream_release_id"] != release["id"]:
            raise RuntimeError("Existing custom tag belongs to a different upstream snapshot")
        version_code = record["version_code"]
    else:
        previous_file = control / ".fork/build.json"
        previous = json.loads(previous_file.read_text()) if previous_file.exists() else {}
        previous_tag = previous.get("upstream_tag")
        if previous_tag and tuple(map(int, STABLE.fullmatch(previous_tag).groups())) > tuple(map(int, STABLE.fullmatch(upstream_tag).groups())):
            raise RuntimeError("Refusing to downgrade the default branch to an older stable release")
        run("git", "worktree", "add", "--detach", source, upstream_sha, cwd=control)
        patch_sources(source)
        workflows = source / ".github/workflows"
        shutil.rmtree(workflows)
        workflows.mkdir()
        shutil.copy2(control / ".github/workflows/android-arm64.yml", workflows / "android-arm64.yml")
        maintenance = source / ".fork"
        if maintenance.exists():
            shutil.rmtree(maintenance)
        shutil.copytree(control / ".fork", maintenance)
        pubspec_path = source / "pubspec.yaml"
        pubspec = pubspec_path.read_text()
        match = re.search(r"^version: (\d+\.\d+\.\d+)\+(\d+)$", pubspec, re.M)
        if not match or match[1] != upstream_tag[1:]:
            raise RuntimeError("Upstream pubspec version does not match its stable tag")
        clock_code = int(datetime.now(timezone.utc).strftime("%Y%m%d%H"))
        version_code = max(int(match[2]), clock_code, int(previous.get("version_code", 0)) + 1)
        if version_code > 2100000000:
            raise RuntimeError("Android versionCode exceeds its maximum")
        pubspec_path.write_text(pubspec[:match.start()] + f"version: {match[1]}+{version_code}" + pubspec[match.end():])
        save(maintenance / "build.json", dict(upstream_tag=upstream_tag, upstream_sha=upstream_sha,
             upstream_release_id=release["id"], fork_tag=fork_tag, version_code=version_code))
        run("git", "add", "-A", cwd=source)
        tree = run("git", "write-tree", cwd=source)
        custom_sha = run("git", "commit-tree", tree, "-p", control_sha, "-m",
                         f"Build Android arm64 from {UPSTREAM}@{upstream_tag} ({upstream_sha})", cwd=source)
        run("git", "checkout", "--detach", custom_sha, cwd=source)
    check_sources(source)
    run("git", "-c", "url.https://github.com/.insteadOf=git@github.com:",
        "submodule", "update", "--init", "--recursive", cwd=source)
    context = dict(source=str(source), branch=branch, control_sha=control_sha,
                   custom_sha=custom_sha, fork_tag=fork_tag, upstream=release,
                   version_code=version_code)
    save(context_path(), context)
    output(skip="false", source=str(source), tag=fork_tag, **versions)
    print(f"Prepared {upstream_tag} as {fork_tag}; versionCode={version_code}")


def android_tool(name):
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ["ANDROID_SDK_ROOT"])
    choices = list((sdk / "build-tools").glob(f"*/{name}"))
    if not choices:
        raise RuntimeError(f"Android SDK tool missing: {name}")
    return max(choices, key=lambda p: [int(x) for x in re.findall(r"\d+", p.parent.name)])


def verify():
    context = json.loads(context_path().read_text())
    source = Path(context["source"])
    check_sources(source)
    if json.loads((source / "env.json").read_text()) != {"APP_ENV": "stable"}:
        raise RuntimeError("APP_ENV must be stable")
    dist = source / "dist"
    manifest = json.loads((dist / "manifest.json").read_text())
    entries = manifest["artifacts"]
    if len(entries) != 1:
        raise RuntimeError("Expected exactly one release artifact")
    entry = entries[0]
    version = context["fork_tag"][1:]
    name = f"FlClash-dev-{version}-android-arm64.apk"
    checksums = f"FlClash-dev-{version}-SHA256SUMS"
    assert entry["name"] == name and entry["os"] == "android" and entry["arch"] == "arm64"
    assert entry["version"] == version and entry["channel"] == "stable"
    assert entry["commit"] == context["custom_sha"]
    assert manifest["checksums"] == checksums
    assert [p.name for p in dist.glob("*.apk")] == [name]
    apk = dist / name
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    assert digest == entry["sha256"]
    assert (dist / checksums).read_text() == f"{digest}  {name}\n"
    with zipfile.ZipFile(apk) as archive:
        assert archive.testzip() is None
        abis = {n.split("/")[1] for n in archive.namelist() if n.startswith("lib/") and n.endswith(".so")}
        assert abis == {"arm64-v8a"}, abis
    badging = run(android_tool("aapt"), "dump", "badging", apk)
    package = re.search(r"^package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging, re.M)
    assert package and package[1] == "com.follow.clash"
    assert package[2] == str(context["version_code"])
    assert package[3] == context["upstream"]["tag_name"][1:]
    apkanalyzer = Path(os.environ["ANDROID_HOME"]) / "cmdline-tools/latest/bin/apkanalyzer"
    xml = run(apkanalyzer, "manifest", "print", apk)
    app = ET.fromstring(xml).find("application")
    items = [e for e in app.findall("meta-data") if e.get(ANDROID + "name") == IMPELLER]
    assert len(items) == 1 and items[0].get(ANDROID + "value") == "false"
    signature = run(android_tool("apksigner"), "verify", "--verbose", "--print-certs", apk)
    actual = re.findall(r"Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]+)", signature)
    expected = os.environ["EXPECTED_CERT_SHA256"].lower()
    assert len(actual) == 1 and actual[0].lower() == expected
    context["assets"] = [str(apk), str(dist / checksums), str(dist / "manifest.json")]
    context["verified"] = True
    save(context_path(), context)
    print("Verified ARM64 only, Impeller=false, stable UI, app identity, version, fixed signer, and update checksums")


def publish():
    context = json.loads(context_path().read_text())
    if not context.get("verified"):
        raise RuntimeError("The APK must pass verification before publication")
    source = Path(context["source"])
    tag = context["fork_tag"]
    upstream = api(f"repos/{UPSTREAM}/releases/tags/{context['upstream']['tag_name']}")
    if upstream["id"] != context["upstream"]["id"] or upstream["draft"] or upstream["prerelease"]:
        raise RuntimeError("Upstream release changed identity or stability during the build")
    existing = api(f"repos/{FORK}/releases/tags/{tag}", missing=True)
    if existing and not existing["draft"]:
        raise RuntimeError("Refusing to replace an already published release")
    # Non-force push: concurrent manual updates fail here, before any release is public.
    if run("git", "rev-parse", "HEAD", cwd=source) != context["custom_sha"]:
        raise RuntimeError("Build worktree HEAD changed")
    remote_branch = run("git", "ls-remote", "origin", f"refs/heads/{context['branch']}", cwd=source).split()[0]
    if remote_branch not in {context["control_sha"], context["custom_sha"]}:
        raise RuntimeError("Default branch changed during the build; retry from its new HEAD")
    run("git", "push", "origin", f"{context['custom_sha']}:refs/heads/{context['branch']}", cwd=source)
    remote_tag = api(f"repos/{FORK}/git/ref/tags/{tag}", missing=True)
    if remote_tag:
        if remote_tag["object"]["type"] != "commit" or remote_tag["object"]["sha"] != context["custom_sha"]:
            raise RuntimeError("Existing custom tag does not point to this source commit")
    else:
        run("git", "tag", tag, context["custom_sha"], cwd=source)
        run("git", "push", "origin", f"refs/tags/{tag}", cwd=source)
    notes = Path(os.environ["RUNNER_TEMP"]) / "upstream-release-body.md"
    notes.write_bytes((upstream["body"] or "").encode("utf-8"))
    title = upstream["name"]
    if not isinstance(title, str) or not title:
        raise RuntimeError("Upstream release title is empty; review rather than invent a title")
    if existing:
        run("gh", "release", "edit", tag, "--repo", FORK, "--title", title,
            "--notes-file", notes, "--prerelease=false", "--draft=true")
        run("gh", "release", "upload", tag, *context["assets"], "--repo", FORK, "--clobber")
    else:
        run("gh", "release", "create", tag, *context["assets"], "--repo", FORK,
            "--target", context["custom_sha"], "--title", title,
            "--notes-file", notes, "--draft", "--prerelease=false")
    draft = api(f"repos/{FORK}/releases/tags/{tag}")
    assert draft["draft"] and not draft["prerelease"]
    assert (draft["body"] or "") == (upstream["body"] or "") and draft["name"] == title
    assert {a["name"] for a in draft["assets"]} == {Path(p).name for p in context["assets"]}
    with tempfile.TemporaryDirectory(prefix="verify-release-", dir=os.environ["RUNNER_TEMP"]) as directory:
        for filename in context["assets"]:
            path = Path(filename)
            run("gh", "release", "download", tag, "--repo", FORK,
                "--pattern", path.name, "--dir", directory)
            assert (Path(directory) / path.name).read_bytes() == path.read_bytes()
    fresh = api(f"repos/{UPSTREAM}/releases/tags/{upstream['tag_name']}")
    if fresh["body"] != upstream["body"] or fresh["name"] != upstream["name"]:
        raise RuntimeError("Upstream notes changed during draft verification; retry")
    run("gh", "release", "edit", tag, "--repo", FORK, "--draft=false", "--prerelease=false", "--latest")
    published = api(f"repos/{FORK}/releases/tags/{tag}")
    assert not published["draft"] and not published["prerelease"]
    assert (published["body"] or "") == (upstream["body"] or "") and published["name"] == title
    # The public release is authoritative deduplication state. This record is only written afterward.
    save(source / ".fork/published.json", dict(upstream_tag=upstream["tag_name"],
         upstream_release_id=upstream["id"], fork_tag=tag, custom_sha=context["custom_sha"],
         version_code=context["version_code"], release_url=published["html_url"]))
    run("git", "add", ".fork/published.json", cwd=source)
    run("git", "commit", "-m", f"Record published Android release {tag}", cwd=source)
    run("git", "push", "origin", f"HEAD:refs/heads/{context['branch']}", cwd=source)
    print(published["html_url"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "verify", "publish", "check-source"])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.command == "check-source":
        check_sources(args.repo)
    else:
        {"prepare": prepare, "verify": verify, "publish": publish}[args.command]()
