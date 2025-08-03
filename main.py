import os
import pandas as pd
import re
from github import Github
from itertools import cycle
from dotenv import load_dotenv
import time
from urllib.parse import urlparse
from collections import deque
import json

load_dotenv()

TOKENS = [
    os.getenv("GITHUB_TOKEN_DH"),
    os.getenv("GITHUB_TOKEN_GN"),
    os.getenv("GITHUB_TOKEN_HJ"),
    os.getenv("GITHUB_TOKEN_SH"),
    os.getenv("GITHUB_TOKEN_SI")
]

if not all(TOKENS):
    raise ValueError("모든 토큰이 .env 파일에 정의되어 있어야 합니다.")

token_cycle = cycle(TOKENS)

# 베이스 디렉토리
base_dir = "all_dependencies_tree"
os.makedirs(base_dir, exist_ok=True)

# 전역 상태 관리
processed_repos = set()
failed_repos = set()
dependency_graph = {}  # 의존성 그래프 저장
stats = {
    'total_repos': 0,
    'successful_downloads': 0,
    'failed_downloads': 0,
    'dependencies_found': 0,
    'start_time': None,
    'end_time': None
}


def get_github_instance():
    return Github(next(token_cycle))


def extract_repo_info(github_url):
    """GitHub URL에서 owner/repo 정보 추출"""
    if github_url.endswith('.git'):
        github_url = github_url[:-4]

    parsed = urlparse(github_url)
    path_parts = parsed.path.strip('/').split('/')
    if len(path_parts) >= 2:
        owner = path_parts[0]
        repo = path_parts[1]
        return owner, repo
    return None, None


def get_default_branch(repository):
    """리포지토리의 기본 브랜치 확인"""
    try:
        return repository.default_branch
    except:
        return None


def extract_dependencies_from_content(content):
    """Package.swift 내용에서 dependencies 추출 (다양한 버전 제약 조건 포함)"""
    dependencies = []

    # dependencies 섹션 찾기
    dependencies_pattern = r'dependencies:\s*\[(.*?)\]'
    match = re.search(dependencies_pattern, content, re.DOTALL)

    if match:
        deps_content = match.group(1)

        # 다양한 .package 패턴들 찾기
        package_patterns = [
            # 기본 패턴: .package(url: "...", ...)
            r'\.package\s*\(\s*url:\s*["\']([^"\']+)["\'][^)]*\)',
            # 축약 패턴: .package("...", ...)
            r'\.package\s*\(\s*["\']([^"\']+)["\'][^)]*\)',
        ]

        found_urls = set()  # 중복 제거

        for pattern in package_patterns:
            urls = re.findall(pattern, deps_content)
            for url in urls:
                if 'github.com' in url:
                    found_urls.add(url)

        dependencies = list(found_urls)

        # 버전 정보도 함께 추출 (분석용)
        if dependencies:
            print(f"    🔍 발견된 의존성 패턴들:")

            # .package 전체 구문들 찾기
            full_package_pattern = r'\.package\s*\([^)]+\)'
            package_declarations = re.findall(full_package_pattern, deps_content, re.DOTALL)

            for i, decl in enumerate(package_declarations, 1):
                # URL 추출
                url_match = re.search(r'url:\s*["\']([^"\']+)["\']|["\']([^"\']+)["\']', decl)
                if url_match:
                    url = url_match.group(1) or url_match.group(2)
                    if 'github.com' in url:
                        # 버전 제약 조건 분석
                        version_info = analyze_version_constraint(decl)
                        print(f"      {i}. {url}")
                        if version_info:
                            print(f"         └─ 버전: {version_info}")

    return dependencies


def analyze_version_constraint(package_declaration):
    """Package 선언에서 버전 제약 조건 분석"""
    version_patterns = {
        'from': r'from:\s*["\']([^"\']+)["\']',
        'upToNextMajor': r'\.upToNextMajor\s*\(\s*from:\s*["\']([^"\']+)["\']\s*\)',
        'upToNextMinor': r'\.upToNextMinor\s*\(\s*from:\s*["\']([^"\']+)["\']\s*\)',
        'exact': r'\.exact\s*\(\s*["\']([^"\']+)["\']\s*\)',
        'range': r'["\']([^"\']+)["\'].+?["\']([^"\']+)["\']',
        'closedRange': r'["\']([^"\']+)["\']\.\.\.["\']\s*([^"\']+)["\']',
        'rangeOperator': r'["\']([^"\']+)["\']\s*\.\.\.?\s*["\']([^"\']+)["\']',
        'branch': r'branch:\s*["\']([^"\']+)["\']',
        'revision': r'revision:\s*["\']([^"\']+)["\']',
    }

    for constraint_type, pattern in version_patterns.items():
        match = re.search(pattern, package_declaration)
        if match:
            if constraint_type == 'from':
                return f"from {match.group(1)}"
            elif constraint_type == 'upToNextMajor':
                version = match.group(1)
                return f"from {version} up to next major (< {int(version.split('.')[0]) + 1}.0.0)"
            elif constraint_type == 'upToNextMinor':
                version = match.group(1)
                parts = version.split('.')
                next_minor = f"{parts[0]}.{int(parts[1]) + 1}.0"
                return f"from {version} up to next minor (< {next_minor})"
            elif constraint_type == 'exact':
                return f"exact {match.group(1)}"
            elif constraint_type in ['range', 'closedRange', 'rangeOperator']:
                if match.lastindex >= 2:
                    return f"range {match.group(1)} ... {match.group(2)}"
            elif constraint_type == 'branch':
                return f"branch: {match.group(1)}"
            elif constraint_type == 'revision':
                return f"revision: {match.group(1)[:8]}..."

    # 기본적인 버전 문자열 찾기
    version_match = re.search(r'["\'](\d+\.\d+\.\d+[^"\']*)["\']', package_declaration)
    if version_match:
        return f"version {version_match.group(1)}"

    return "버전 제약 조건 없음"


def create_directory_path(repo_key, root_repo_key):
    """리포지토리의 디렉토리 경로 생성"""
    safe_root = root_repo_key.replace('/', '_')
    safe_name = repo_key.replace('/', '_')
    return os.path.join(base_dir, safe_root, safe_name)


def download_package_swift(github_url, root_repo_key, depth=0):
    """Package.swift 파일 다운로드 (완전 재귀 버전)"""
    indent = "  " * min(depth, 10)  # 너무 많은 들여쓰기 방지

    try:
        owner, repo = extract_repo_info(github_url)
        if not owner or not repo:
            return False

        repo_key = f"{owner}/{repo}"

        # 이미 처리된 리포지토리인지 확인 (순환 참조 방지)
        if repo_key in processed_repos or repo_key in failed_repos:
            if depth > 0:  # 루트가 아닌 경우만 스킵 메시지
                print(f"{indent}🔄 {repo_key}: 이미 처리됨 (깊이: {depth})")
            return True

        print(f"{indent}🔍 [{len(processed_repos) + 1}] {repo_key} 처리 중... (깊이: {depth})")

        g = get_github_instance()
        repository = g.get_repo(repo_key)

        # 브랜치 시도
        branches_to_try = []
        default_branch = get_default_branch(repository)
        if default_branch:
            branches_to_try.append(default_branch)

        for branch in ['main', 'master']:
            if branch not in branches_to_try:
                branches_to_try.append(branch)

        package_file = None
        used_branch = None

        for branch in branches_to_try:
            try:
                package_file = repository.get_contents("Package.swift", ref=branch)
                used_branch = branch
                break
            except:
                continue

        if package_file is None:
            failed_repos.add(repo_key)
            print(f"{indent}❌ {repo_key}: Package.swift 없음")
            return False

        # 디렉토리 생성
        repo_dir = create_directory_path(repo_key, root_repo_key)
        os.makedirs(repo_dir, exist_ok=True)

        # Package.swift 파일 저장
        package_path = os.path.join(repo_dir, "Package.swift")
        content = package_file.decoded_content.decode('utf-8')

        with open(package_path, 'w', encoding='utf-8') as f:
            f.write(content)

        processed_repos.add(repo_key)
        stats['successful_downloads'] += 1

        print(f"{indent}✅ {repo_key} 다운로드 완료 (브랜치: {used_branch})")

        # 의존성 추출
        dependencies = extract_dependencies_from_content(content)

        if dependencies:
            stats['dependencies_found'] += len(dependencies)
            if repo_key not in dependency_graph:
                dependency_graph[repo_key] = []

            print(f"{indent}📦 {repo_key}: {len(dependencies)}개 의존성 발견")

            # 의존성 처리 (완전 재귀 - 제한 없음)
            for i, dep_url in enumerate(dependencies, 1):
                dep_owner, dep_repo = extract_repo_info(dep_url)
                if dep_owner and dep_repo:
                    dep_key = f"{dep_owner}/{dep_repo}"
                    dependency_graph[repo_key].append(dep_key)

                    print(f"{indent}  └─ {i}/{len(dependencies)}: {dep_key}")

                    # 재귀 호출 (깊이 제한 없음!)
                    download_package_swift(dep_url, root_repo_key, depth + 1)
                    time.sleep(0.2)  # API 레이트 리밋 방지 (더 빠르게)
        else:
            print(f"{indent}📦 {repo_key}: 의존성 없음")

        return True

    except Exception as e:
        if 'repo_key' in locals():
            failed_repos.add(repo_key)
            print(f"{indent}❌ {repo_key}: {str(e)[:50]}...")
        stats['failed_downloads'] += 1
        return False


def process_all_repositories():
    """CSV의 모든 리포지토리 처리"""
    csv_file = "swift_spm_networking_repos.csv"

    try:
        df = pd.read_csv(csv_file)
    except FileNotFoundError:
        print(f"❌ CSV 파일을 찾을 수 없습니다: {csv_file}")
        return

    stats['total_repos'] = len(df)
    stats['start_time'] = time.time()

    print(f"🚀 총 {len(df)}개의 리포지토리 처리를 시작합니다!")
    print(f"📁 저장 위치: {os.path.abspath(base_dir)}")
    print("=" * 80)

    # 진행률 표시를 위한 변수
    processed_count = 0

    for index, row in df.iterrows():
        processed_count += 1
        github_url = row['url']
        repo_name = row['repo']

        # 진행률 표시
        progress = (processed_count / len(df)) * 100
        print(f"\n📊 진행률: {processed_count}/{len(df)} ({progress:.1f}%)")
        print(f"🔍 [{processed_count}] {repo_name} 처리 중...")

        # 루트 리포지토리로 처리
        owner, repo = extract_repo_info(github_url)
        if owner and repo:
            root_repo_key = f"{owner}/{repo}"
            download_package_swift(github_url, root_repo_key, 0)

        # 진행 상황 출력
        if processed_count % 10 == 0:
            elapsed = time.time() - stats['start_time']
            avg_time = elapsed / processed_count
            estimated_total = avg_time * len(df)
            remaining = estimated_total - elapsed

            print(f"⏱️  경과 시간: {elapsed / 60:.1f}분, 예상 남은 시간: {remaining / 60:.1f}분")
            print(
                f"📈 현재 성공률: {len(processed_repos)}/{processed_count} ({len(processed_repos) / processed_count * 100:.1f}%)")

        # API 레이트 리밋을 위한 대기
        time.sleep(0.5)

    stats['end_time'] = time.time()


def create_final_summary():
    """최종 요약 리포트 생성"""
    summary_path = os.path.join(base_dir, "FINAL_SUMMARY.md")

    elapsed_time = stats['end_time'] - stats['start_time']

    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write("# Swift Package Dependencies - Final Summary\n\n")
        f.write(f"**Generated**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**Processing Time**: {elapsed_time / 60:.2f} minutes\n\n")

        f.write("## Statistics\n\n")
        f.write(f"- **Total Repositories in CSV**: {stats['total_repos']}\n")
        f.write(f"- **Successfully Downloaded**: {stats['successful_downloads']}\n")
        f.write(f"- **Failed Downloads**: {stats['failed_downloads']}\n")
        f.write(f"- **Success Rate**: {stats['successful_downloads'] / stats['total_repos'] * 100:.1f}%\n")
        f.write(f"- **Total Dependencies Found**: {stats['dependencies_found']}\n")
        f.write(f"- **Unique Repositories Processed**: {len(processed_repos)}\n\n")

        f.write("## Successfully Processed Repositories\n\n")
        for repo in sorted(processed_repos):
            f.write(f"- {repo}\n")

        if failed_repos:
            f.write(f"\n## Failed Repositories ({len(failed_repos)})\n\n")
            for repo in sorted(failed_repos):
                f.write(f"- {repo}\n")

        f.write("\n## Dependency Relationships\n\n")
        for repo, deps in dependency_graph.items():
            if deps:
                f.write(f"**{repo}**:\n")
                for dep in deps:
                    f.write(f"  - {dep}\n")
                f.write("\n")

    # JSON 형태로도 저장
    json_path = os.path.join(base_dir, "dependency_graph.json")
    with open(json_path, 'w') as f:
        json.dump({
            'stats': stats,
            'dependency_graph': dependency_graph,
            'processed_repos': list(processed_repos),
            'failed_repos': list(failed_repos)
        }, f, indent=2)

    print(f"📊 최종 요약이 생성되었습니다:")
    print(f"   📄 {summary_path}")
    print(f"   📄 {json_path}")


def main():
    print("🌍 Swift Package 전체 의존성 분석기")
    print("=" * 50)

    # 기존 데이터 확인
    if os.path.exists(base_dir) and os.listdir(base_dir):
        print(f"⚠️  기존 데이터가 발견되었습니다: {base_dir}")
        response = input("기존 데이터를 삭제하고 새로 시작하시겠습니까? (y/N): ").strip().lower()

        if response == 'y' or response == 'yes':
            import shutil
            shutil.rmtree(base_dir)
            os.makedirs(base_dir, exist_ok=True)
            print("🗑️  기존 데이터를 삭제했습니다.")
        else:
            print("🔄 기존 데이터에 추가로 처리합니다.")

    # 모든 리포지토리 처리
    process_all_repositories()

    # 최종 요약 생성
    create_final_summary()

    print("\n" + "=" * 80)
    print("🎉 전체 처리 완료!")
    print(f"📊 총 처리 시간: {(stats['end_time'] - stats['start_time']) / 60:.2f}분")
    print(f"✅ 성공: {stats['successful_downloads']}개")
    print(f"❌ 실패: {stats['failed_downloads']}개")
    print(f"📈 성공률: {stats['successful_downloads'] / stats['total_repos'] * 100:.1f}%")
    print(f"📁 결과 위치: {os.path.abspath(base_dir)}")


if __name__ == "__main__":
    main()