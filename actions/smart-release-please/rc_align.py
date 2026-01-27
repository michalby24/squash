import os
import re
import subprocess
import sys

BOT_COMMIT_MSG = "chore: enforce correct rc version"
BOT_FOOTER_TAG = "Release-As:"

def run_git_command(args, fail_on_error=True):
    try:
        result = subprocess.run(["git"] + args, stdout=subprocess.PIPE, text=True, check=fail_on_error)
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None

def parse_semver(tag):
    if not tag:
        return 0, 0, 0, 0

    # Match RC: v1.0.0-rc.1
    m_rc = re.match(r"^v(\d+)\.(\d+)\.(\d+)-rc\.(\d+)$", tag)
    if m_rc:
        return int(m_rc[1]), int(m_rc[2]), int(m_rc[3]), int(m_rc[4])

    # Match Stable: v1.0.0
    m_stable = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", tag)
    if m_stable:
        # Return rc=0 for stable, but logic elsewhere distinguishes stable vs rc
        return int(m_stable[1]), int(m_stable[2]), int(m_stable[3]), 0
    
    return 0, 0, 0, 0

def find_baseline_tag():
    # 1. Fetch all tags from remote to ensure we see tags from all branches
    run_git_command(["fetch", "--tags"], fail_on_error=False)
    
    # 2. Get ALL tags from the repository
    tags_output = run_git_command(
        ["tag", "-l", "v*"], 
        fail_on_error=False
    )
    
    if not tags_output:
        print("INFO: No tags found in repository. Assuming 0.0.0 baseline.")
        return None, True
    
    all_tags = tags_output.split('\n')
    
    # 2. Python-side Sort (Reliable SemVer)
    # Returns tuple: (major, minor, patch, is_stable, rc_num)
    # is_stable is 1 for Stable, 0 for RC. This GUARANTEES Stable > RC for same version.
    def version_key(t):
        maj, min, pat, rc = parse_semver(t)
        is_stable = 1 if "-rc" not in t else 0
        return (maj, min, pat, is_stable, rc)

    # Sort descending (Highest version first)
    sorted_tags = sorted(all_tags, key=version_key, reverse=True)
    
    best_tag = sorted_tags[0]

    # Debug output to verify what we found
    print(f"DEBUG: Top 3 tags found: {sorted_tags[:3]}")

    if "-rc" in best_tag:
        print(f"INFO: Baseline found (RC): {best_tag}")
        return best_tag, False
    
    print(f"INFO: Baseline found (Stable): {best_tag}")
    return best_tag, True

def get_commit_depth(baseline_tag):
    rev_range = f"{baseline_tag}..HEAD" if baseline_tag else "HEAD"
    
    print(f"INFO: Analyzing commit range: {rev_range}")
    
    raw_subjects = run_git_command(["log", rev_range, "--first-parent", "--pretty=format:%s"], fail_on_error=False)
    if not raw_subjects:
        return 0

    real_commits = []
    filtered_commits = []
    for s in raw_subjects.split('\n'):
        if BOT_FOOTER_TAG in s or BOT_COMMIT_MSG in s:
            filtered_commits.append(s)
            continue
        
        if re.match(r"^chore(\(.*\))?: release", s):
            filtered_commits.append(s)
            continue
        
        if "chore: reset manifest to stable version" in s:
            filtered_commits.append(s)
            continue
            
        real_commits.append(s)

    if filtered_commits:
        print(f"INFO: Filtered out {len(filtered_commits)} bot/release commits")
    print(f"INFO: Found {len(real_commits)} user commits since {baseline_tag or 'start'}")
    
    return len(real_commits)

def analyze_impact_from_latest_commit(baseline_tag):
    """
    Analyze impact based on the LATEST meaningful commit, not all commits.
    This is critical for squash-and-merge scenarios where we only care about
    the most recent change type.
    """
    rev_range = f"{baseline_tag}..HEAD" if baseline_tag else "HEAD"
    
    # Get all commit subjects in chronological order (oldest first)
    raw_subjects = run_git_command(
        ["log", rev_range, "--first-parent", "--pretty=format:%s", "--reverse"], 
        fail_on_error=False
    )
    
    if not raw_subjects:
        return False, False
    
    # Filter out bot/release commits to find the last REAL user commit
    real_commits = []
    for s in raw_subjects.split('\n'):
        # Skip bot commits
        if BOT_FOOTER_TAG in s or BOT_COMMIT_MSG in s:
            continue
        if re.match(r"^chore(\(.*\))?: release", s):
            continue
        if "chore: reset manifest to stable version" in s:
            continue
        if "chore: update manifest to" in s:
            continue
            
        real_commits.append(s)
    
    if not real_commits:
        print("INFO: No real commits found for impact analysis")
        return False, False
    
    # Get the LAST real commit (most recent)
    latest_commit = real_commits[-1]
    print(f"INFO: Analyzing impact from latest commit: '{latest_commit}'")
    
    # Also need to check the full body of the latest commit for BREAKING CHANGE
    latest_commit_body = run_git_command(
        ["log", "-1", "--pretty=format:%B"], 
        fail_on_error=False
    )
    
    # Check for breaking change patterns
    breaking_regex = r"^(feat|fix|refactor)(\(.*\))?!:"
    is_breaking = (
        re.search(breaking_regex, latest_commit) or 
        "BREAKING CHANGE" in latest_commit_body
    )
    
    # Check for feature
    is_feat = re.search(r"^feat(\(.*\))?:", latest_commit)
    
    result_breaking = bool(is_breaking)
    result_feat = bool(is_feat)
    
    print(f"INFO: Latest commit impact - breaking={result_breaking}, feat={result_feat}")
    
    return result_breaking, result_feat

def analyze_impact(baseline_tag):
    """
    Legacy function - analyzes ALL commits since baseline.
    Kept for backward compatibility but should use analyze_impact_from_latest_commit
    for squash scenarios.
    """
    rev_range = f"{baseline_tag}..HEAD" if baseline_tag else "HEAD"
    logs = run_git_command(["log", rev_range, "--pretty=format:%B"], fail_on_error=False)
    
    if not logs:
        return False, False

    breaking_regex = r"^(feat|fix|refactor)(\(.*\))?!:"
    is_breaking = re.search(breaking_regex, logs, re.MULTILINE) or "BREAKING CHANGE" in logs
    is_feat = re.search(r"^feat(\(.*\))?:", logs, re.MULTILINE)

    return bool(is_breaking), bool(is_feat)

def calculate_next_version(major, minor, patch, rc, depth, is_breaking, is_feat, from_stable):
    """
    Calculate the next version based on commit impact and baseline state.
    
    KEY FIX: When coming from stable, ALWAYS start with -rc.1 (not -rc.{depth})
    Only increment the RC number when already on an RC tag.
    """
    
    # If Breaking -> major+1.0.0-rc.1
    if is_breaking:
        return f"{major + 1}.0.0-rc.1"
    
    # If Feature
    if is_feat:
        if from_stable:
            # From stable v0.1.0 → v0.2.0-rc.1 (always start with rc.1)
            return f"{major}.{minor + 1}.0-rc.1"
        else:
            # Already on RC, check if we need to bump minor or just increment RC
            if patch > 0:
                # We're on something like 0.1.1-rc.3, new feat bumps to 0.2.0-rc.1
                return f"{major}.{minor + 1}.0-rc.1"
            else:
                # We're on something like 0.2.0-rc.3, stay on 0.2.0 and increment RC
                return f"{major}.{minor}.{patch}-rc.{rc + depth}"

    # If Fix (or any other change)
    if from_stable:
        # From stable v0.1.0 → v0.1.1-rc.1 (always start with rc.1)
        return f"{major}.{minor}.{patch + 1}-rc.1"
    else:
        # Already on RC v0.1.1-rc.3 → v0.1.1-rc.{3+depth}
        return f"{major}.{minor}.{patch}-rc.{rc + depth}"

def main():
    branch = os.environ.get("GITHUB_REF_NAME")
    print(f"INFO: Running on branch: {branch}")

    last_commit_msg = run_git_command(["log", "-1", "--pretty=%s"], fail_on_error=False)
    
    # Check if next branch has a stable release tag at HEAD (happens after sync from main)
    if branch == "next":
        head_tags = run_git_command(["tag", "--points-at", "HEAD"], fail_on_error=False)
        if head_tags:
            for tag in head_tags.split('\n'):
                if tag.startswith('v') and re.match(r'^v\d+\.\d+\.\d+$', tag):
                    print(f"INFO: next branch has stable tag '{tag}' at HEAD. Skipping (likely just synced from main).")
                    return
    
    # Check for release-please release commits
    if last_commit_msg and re.match(r"^chore(\(.*\))?: release", last_commit_msg):
        print(f"INFO: Detected release-please release commit: '{last_commit_msg}'. Skipping.")
        return
    
    # Check for merge commits from release-please branches
    if last_commit_msg and "Merge pull request" in last_commit_msg and "release-please" in last_commit_msg:
        print(f"INFO: Detected release-please merge commit: '{last_commit_msg}'. Skipping.")
        return
    
    # Check for manifest reset commits (happens after stable release on main)
    if last_commit_msg and "chore: reset manifest to stable version" in last_commit_msg:
        print(f"INFO: Detected manifest reset commit: '{last_commit_msg}'. Skipping.")
        return

    # --- LOGIC FOR MAIN (Stable Promotion) ---
    if branch in ["main", "master"]:
        try:
            # Fetch next branch explicitly to get its tags (critical for squash merge)
            print("INFO: Fetching next branch and all tags from remote...")
            run_git_command(["fetch", "origin", "next"], fail_on_error=False)
            run_git_command(["fetch", "origin", "main"], fail_on_error=False)
            run_git_command(["fetch", "--tags", "--force"], fail_on_error=False)
            
            # Get ALL tags from the repository
            tags_output = run_git_command(["tag", "-l", "v*"], fail_on_error=False)
            
            if not tags_output:
                stable_version = "0.1.0"
                print(f"INFO: No tags found, defaulting to {stable_version}")
            else:
                all_tags = tags_output.split('\n')
                # Reuse sort logic
                def version_key(t):
                    maj, min, pat, rc = parse_semver(t)
                    is_stable = 1 if "-rc" not in t else 0
                    return (maj, min, pat, is_stable, rc)

                latest_tag = sorted(all_tags, key=version_key, reverse=True)[0]
                print(f"INFO: Latest tag found: {latest_tag}")
                print(f"DEBUG: All tags sorted: {sorted(all_tags, key=version_key, reverse=True)[:5]}")
                
                # Strip RC suffix to get stable version
                # Handle both -rc.X and -rcX formats
                clean_tag = re.sub(r'-rc(\.\d+)?$', '', latest_tag)
                stable_version = clean_tag.lstrip('v')
                print(f"INFO: After stripping RC from '{latest_tag}': '{clean_tag}'")
                print(f"INFO: Promoting to stable: {stable_version}")

            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"next_version={stable_version}\n")
            print(f"OUTPUT: next_version={stable_version}")
            return

        except Exception as e:
            print(f"CRITICAL ERROR (stable): {e}")
            sys.exit(0)

    # --- LOGIC FOR NEXT (RC Calculation) ---
    try:
        tag, from_stable = find_baseline_tag()
        
        depth = get_commit_depth(tag)
        if depth == 0:
            print("INFO: No user commits found since baseline. Exiting.")
            return

        major, minor, patch, rc = parse_semver(tag)
        
        # KEY FIX: Use latest commit analysis for squash scenarios
        is_breaking, is_feat = analyze_impact_from_latest_commit(tag)

        print(f"INFO: Baseline version: {tag or '0.0.0'} (from_stable={from_stable})")
        print(f"INFO: Impact analysis - breaking={is_breaking}, feat={is_feat}, depth={depth}")

        next_ver = calculate_next_version(
            major, minor, patch, rc, 
            depth, is_breaking, is_feat, from_stable
        )

        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"next_version={next_ver}\n")
        
        print(f"OUTPUT: next_version={next_ver}")

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
        sys.exit(0)

if __name__ == "__main__":
    main()
