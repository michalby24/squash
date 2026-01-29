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
    
    m_rc = re.match(r"^v(\d+)\.(\d+)\.(\d+)-rc\.(\d+)$", tag)
    if m_rc:
        return int(m_rc[1]), int(m_rc[2]), int(m_rc[3]), int(m_rc[4])
    
    m_stable = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", tag)
    if m_stable:
        return int(m_stable[1]), int(m_stable[2]), int(m_stable[3]), 0
    
    return 0, 0, 0, 0

def find_baseline_tag():
    run_git_command(["fetch", "--tags"], fail_on_error=False)
    tags_output = run_git_command(["tag", "-l", "v*"], fail_on_error=False)
    
    if not tags_output:
        print("INFO: No tags found. Assuming 0.0.0 baseline.")
        return None, True
    
    all_tags = tags_output.split('\n')
    
    def version_key(t):
        maj, min, pat, rc = parse_semver(t)
        is_stable = 1 if "-rc" not in t else 0
        return (maj, min, pat, is_stable, rc)
    
    sorted_tags = sorted(all_tags, key=version_key, reverse=True)
    best_tag = sorted_tags[0]
    
    if "-rc" in best_tag:
        print(f"INFO: Baseline (RC): {best_tag}")
        return best_tag, False
    
    print(f"INFO: Baseline (Stable): {best_tag}")
    return best_tag, True

def get_commit_depth(baseline_tag):
    rev_range = f"{baseline_tag}..HEAD" if baseline_tag else "HEAD"
    raw_subjects = run_git_command(["log", rev_range, "--first-parent", "--pretty=format:%s"], fail_on_error=False)
    
    if not raw_subjects:
        return 0
    
    real_commits = []
    for s in raw_subjects.split('\n'):
        if any(x in s for x in [BOT_FOOTER_TAG, BOT_COMMIT_MSG]):
            continue
        if re.match(r"^chore(\(.*\))?: release", s):
            continue
        real_commits.append(s)
    
    print(f"INFO: Found {len(real_commits)} commits since {baseline_tag or 'start'}")
    return len(real_commits)

def analyze_impact_from_latest(baseline_tag):
    rev_range = f"{baseline_tag}..HEAD" if baseline_tag else "HEAD"
    raw_subjects = run_git_command(["log", rev_range, "--first-parent", "--pretty=format:%s", "--reverse"], fail_on_error=False)
    
    if not raw_subjects:
        return False, False
    
    real_commits = []
    for s in raw_subjects.split('\n'):
        if any(x in s for x in [BOT_FOOTER_TAG, BOT_COMMIT_MSG]):
            continue
        if re.match(r"^chore(\(.*\))?: release", s):
            continue
        real_commits.append(s)
    
    if not real_commits:
        return False, False
    
    latest = real_commits[-1]
    latest_body = run_git_command(["log", "-1", "--pretty=format:%B"], fail_on_error=False)
    
    print(f"INFO: Analyzing latest commit: '{latest}'")
    
    breaking_pattern = r"^(feat|fix|refactor)(\(.*\))?!:"
    is_breaking = re.search(breaking_pattern, latest) or "BREAKING CHANGE" in latest_body
    is_feat = re.search(r"^feat(\(.*\))?:", latest)
    
    return bool(is_breaking), bool(is_feat)

def calculate_next_version(major, minor, patch, rc, depth, is_breaking, is_feat, from_stable):
    if is_breaking:
        return f"{major + 1}.0.0-rc.1"
    
    if is_feat:
        if from_stable:
            return f"{major}.{minor + 1}.0-rc.1"
        else:
            if patch > 0:
                return f"{major}.{minor + 1}.0-rc.1"
            else:
                return f"{major}.{minor}.{patch}-rc.{rc + depth}"
    
    if from_stable:
        return f"{major}.{minor}.{patch + 1}-rc.1"
    else:
        return f"{major}.{minor}.{patch}-rc.{rc + depth}"

def main():
    branch = os.environ.get("GITHUB_REF_NAME")
    last_commit = run_git_command(["log", "-1", "--pretty=%s"], fail_on_error=False)
    
    if branch == "next":
        head_tags = run_git_command(["tag", "--points-at", "HEAD"], fail_on_error=False)
        if head_tags:
            for tag in head_tags.split('\n'):
                if tag.startswith('v') and re.match(r'^v\d+\.\d+\.\d+$', tag):
                    print(f"INFO: Stable tag at HEAD. Skipping.")
                    return
    
    skip_patterns = [
        (r"^chore(\(.*\))?: release", "release-please commit"),
        ("release-please", "release-please merge"),
    ]
    
    for pattern, desc in skip_patterns:
        if last_commit and re.search(pattern, last_commit):
            print(f"INFO: Detected {desc}. Skipping.")
            return
    
    if branch in ["main", "master"]:
        try:
            run_git_command(["fetch", "origin", branch], fail_on_error=False)
            run_git_command(["fetch", "--tags", "--force"], fail_on_error=False)
            
            tags_output = run_git_command(["tag", "-l", "v*"], fail_on_error=False)
            
            if not tags_output:
                stable_version = "0.1.0"
            else:
                all_tags = tags_output.split('\n')
                
                def version_key(t):
                    maj, min, pat, rc = parse_semver(t)
                    is_stable = 1 if "-rc" not in t else 0
                    return (maj, min, pat, is_stable, rc)
                
                latest_tag = sorted(all_tags, key=version_key, reverse=True)[0]
                clean_tag = re.sub(r'-rc(\.\d+)?$', '', latest_tag)
                stable_version = clean_tag.lstrip('v')
            
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"next_version={stable_version}\n")
            print(f"OUTPUT: next_version={stable_version}")
            return
        
        except Exception as e:
            print(f"ERROR: {e}")
            sys.exit(1)
    
    try:
        tag, from_stable = find_baseline_tag()
        depth = get_commit_depth(tag)
        
        if depth == 0:
            print("INFO: No commits since baseline. Exiting.")
            return
        
        major, minor, patch, rc = parse_semver(tag)
        is_breaking, is_feat = analyze_impact_from_latest(tag)
        next_ver = calculate_next_version(major, minor, patch, rc, depth, is_breaking, is_feat, from_stable)
        
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"next_version={next_ver}\n")
        
        print(f"OUTPUT: next_version={next_ver}")
    
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
