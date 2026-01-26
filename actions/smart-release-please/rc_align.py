import os
import re
import subprocess
import sys
from typing import List, Optional, Tuple, NamedTuple

# Configuration constants
BOT_COMMIT_MSG = "chore: enforce correct rc version"
BOT_FOOTER_TAG = "Release-As:"
MANIFEST_RESET_MSG = "chore: reset manifest to stable version"

class Version(NamedTuple):
    major: int
    minor: int
    patch: int
    rc: int = 0
    is_stable: bool = True

    @classmethod
    def parse(cls, tag: str) -> "Version":
        if not tag:
            return cls(0, 0, 0, 0, True)
        
        # Match RC: v1.0.0-rc.1
        m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?$", tag)
        if m:
            major, minor, patch = map(int, m.groups()[:3])
            rc = int(m.group(4)) if m.group(4) else 0
            return cls(major, minor, patch, rc, is_stable=m.group(4) is None)
        
        return cls(0, 0, 0, 0, True)
    
    def to_string(self) -> str:
        ver = f"{self.major}.{self.minor}.{self.patch}"
        if not self.is_stable:
            ver += f"-rc.{self.rc}"
        return ver

    def sort_key(self):
        # Sort key ensuring Stable > RC for the same version
        # (major, minor, patch, is_stable, rc)
        return (self.major, self.minor, self.patch, 1 if self.is_stable else 0, self.rc)

def run_git(args: List[str], fail_on_error: bool = True) -> Optional[str]:
    try:
        result = subprocess.run(["git"] + args, stdout=subprocess.PIPE, text=True, check=fail_on_error)
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None

def find_baseline_tag() -> Tuple[Optional[str], bool]:
    # 1. Fetch tags merged into HEAD
    tags_output = run_git(["tag", "-l", "v*", "--merged", "HEAD"], fail_on_error=False)
    
    if not tags_output:
        print("INFO: No tags found in current branch history. Assuming 0.0.0 baseline.")
        return None, True
    
    # 2. Parse and Sort
    all_tags = tags_output.split('\n')
    parsed_tags = [(tag, Version.parse(tag)) for tag in all_tags]
    
    # Sort descending
    parsed_tags.sort(key=lambda x: x[1].sort_key(), reverse=True)
    
    best_tag_str, best_tag_ver = parsed_tags[0]
    
    # Debug output
    top_3 = [t[0] for t in parsed_tags[:3]]
    print(f"DEBUG: Top 3 tags found: {top_3}")
    
    print(f"INFO: Baseline found ({'Stable' if best_tag_ver.is_stable else 'RC'}): {best_tag_str}")
    return best_tag_str, best_tag_ver.is_stable

def get_depth_and_impact(baseline_tag: Optional[str]) -> Tuple[int, bool, bool]:
    rev_range = f"{baseline_tag}..HEAD" if baseline_tag else "HEAD"
    print(f"INFO: Analyzing commit range: {rev_range}")
    
    # Get subjects and bodies
    output = run_git(["log", rev_range, "--first-parent", "--pretty=format:%s|%B%n__SEP__"], fail_on_error=False)
    if not output:
        return 0, False, False

    commits = output.split('\n__SEP__\n')
    real_commits = []
    
    is_breaking = False
    is_feat = False
    
    breaking_regex = r"^(feat|fix|refactor)(\(.*\))?!:"
    feat_regex = r"^feat(\(.*\))?:"

    for commit in commits:
        if not commit.strip():
            continue
            
        parts = commit.split('|', 1)
        subject = parts[0]
        body = parts[1] if len(parts) > 1 else ""
        full_msg = f"{subject}\n{body}"

        # Filtering logic
        if (BOT_FOOTER_TAG in subject or 
            BOT_COMMIT_MSG in subject or 
            re.match(r"^chore(\(.*\))?: release", subject) or 
            MANIFEST_RESET_MSG in subject):
            continue

        real_commits.append(subject)
        
        # Impact Analysis
        if re.search(breaking_regex, full_msg, re.MULTILINE) or "BREAKING CHANGE" in full_msg:
            is_breaking = True
        if re.search(feat_regex, full_msg, re.MULTILINE):
            is_feat = True

    print(f"INFO: Found {len(real_commits)} user commits since {baseline_tag or 'start'}")
    return len(real_commits), is_breaking, is_feat

def calculate_next_version(tag_ver: Version, depth: int, is_breaking: bool, is_feat: bool, from_stable: bool) -> str:
    # Logic:
    # If Breaking -> major+1.0.0
    # If Feature  -> minor+1.0 (if from stable) OR current.minor.patch (if working on RC)
    # If Fix      -> patch+1   (if from stable) OR current rc increment

    if is_breaking:
        return f"{tag_ver.major + 1}.0.0-rc.{depth}"
    
    if is_feat:
        if from_stable or tag_ver.patch > 0:
            return f"{tag_ver.major}.{tag_ver.minor + 1}.0-rc.{depth}"
        else:
            return f"{tag_ver.major}.{tag_ver.minor}.{tag_ver.patch}-rc.{tag_ver.rc + depth}"

    if from_stable:
        return f"{tag_ver.major}.{tag_ver.minor}.{tag_ver.patch + 1}-rc.{depth}"
    else:
        return f"{tag_ver.major}.{tag_ver.minor}.{tag_ver.patch}-rc.{tag_ver.rc + depth}"

def set_output(name: str, value: str):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"{name}={value}\n")
    print(f"OUTPUT: {name}={value}")

def should_skip(branch: str) -> bool:
    last_commit_msg = run_git(["log", "-1", "--pretty=%s"], fail_on_error=False) or ""
    last_commit_body = run_git(["log", "-1", "--pretty=%b"], fail_on_error=False) or ""
    full_commit_msg = f"{last_commit_msg}\n{last_commit_body}"
    
    if branch == "next":
        head_tags = run_git(["tag", "--points-at", "HEAD"], fail_on_error=False)
        if head_tags:
            for tag in head_tags.split('\n'):
                if tag.startswith('v') and re.match(r'^v\d+\.\d+\.\d+$', tag):
                    print(f"INFO: next branch has stable tag '{tag}' at HEAD. Skipping.")
                    return True

    if re.match(r"^chore(\(.*\))?: release", last_commit_msg):
        print(f"INFO: Detected release-please release commit. Skipping.")
        return True
    
    # Detect both regular merge and squash merge from release-please
    if "release-please" in last_commit_msg:
        print(f"INFO: Detected release-please merge commit. Skipping.")
        return True
    
    if MANIFEST_RESET_MSG in last_commit_msg:
        print(f"INFO: Detected manifest reset commit. Skipping.")
        return True
    
    # Skip if this commit already has a Release-As footer (from squash merge)
    if BOT_FOOTER_TAG in full_commit_msg:
        print(f"INFO: Commit already has {BOT_FOOTER_TAG} footer. Skipping.")
        return True
        
    return False

def promote_stable():
    try:
        run_git(["fetch", "--tags"], fail_on_error=False)
        tags_output = run_git(["tag", "-l", "v*"], fail_on_error=False)
        
        if not tags_output:
            stable_version = "0.1.0"
            print(f"INFO: No tags found, defaulting to {stable_version}")
        else:
            all_tags = tags_output.split('\n')
            parsed_tags = [(tag, Version.parse(tag)) for tag in all_tags]
            parsed_tags.sort(key=lambda x: x[1].sort_key(), reverse=True)
            
            latest_tag_str, latest_tag_ver = parsed_tags[0]
            print(f"INFO: Latest tag found: {latest_tag_str}")
            
            # Promote to stable (strip -rc)
            stable_version = f"{latest_tag_ver.major}.{latest_tag_ver.minor}.{latest_tag_ver.patch}"
            print(f"INFO: Promoting to stable {stable_version}")

        set_output("next_version", stable_version)

    except Exception as e:
        print(f"CRITICAL ERROR (stable): {e}")
        sys.exit(1)

def calculate_rc():
    try:
        baseline_tag, from_stable = find_baseline_tag()
        depth, is_breaking, is_feat = get_depth_and_impact(baseline_tag)
        
        if depth == 0:
            print("INFO: No user commits found since baseline. Exiting.")
            return

        baseline_ver = Version.parse(baseline_tag) if baseline_tag else Version(0,0,0,0,True)

        print(f"INFO: Impact analysis - breaking={is_breaking}, feat={is_feat}, depth={depth}")

        next_ver = calculate_next_version(baseline_ver, depth, is_breaking, is_feat, from_stable)
        set_output("next_version", next_ver)

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        sys.exit(1)

def main():
    branch = os.environ.get("GITHUB_REF_NAME")
    print(f"INFO: Running on branch: {branch}")
    
    if should_skip(branch):
        return

    if branch in ["main", "master"]:
        promote_stable()
    else:
        calculate_rc()

if __name__ == "__main__":
    main()