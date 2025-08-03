import os
import json
import re
from collections import defaultdict, deque
from urllib.parse import urlparse
import time


class DependencyGraphAnalyzer:
    def __init__(self, base_dir="all_dependencies_tree"):
        self.base_dir = base_dir
        self.graph = defaultdict(list)  # 간선 정보: repo -> [dependencies]
        self.reverse_graph = defaultdict(list)  # 역방향 그래프: dependency -> [dependents]
        self.nodes = set()  # 정점 정보: 모든 고유한 저장소들
        self.package_info = {}  # 각 저장소의 패키지 정보
        self.stats = {
            'total_nodes': 0,
            'total_edges': 0,
            'root_packages': 0,
            'leaf_packages': 0,
            'max_depth': 0,
            'circular_dependencies': []
        }

    def extract_repo_info_from_url(self, github_url):
        """GitHub URL에서 owner/repo 정보 추출"""
        if github_url.endswith('.git'):
            github_url = github_url[:-4]

        parsed = urlparse(github_url)
        path_parts = parsed.path.strip('/').split('/')
        if len(path_parts) >= 2:
            owner = path_parts[0]
            repo = path_parts[1]
            return f"{owner}/{repo}"
        return None

    def parse_package_swift(self, file_path):
        """Package.swift 파일에서 의존성 정보 추출"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            dependencies = []

            # dependencies 섹션 찾기
            dependencies_pattern = r'dependencies:\s*\[(.*?)\]'
            match = re.search(dependencies_pattern, content, re.DOTALL)

            if match:
                deps_content = match.group(1)

                # 다양한 .package 패턴들 찾기
                package_patterns = [
                    r'\.package\s*\(\s*url:\s*["\']([^"\']+)["\'][^)]*\)',
                    r'\.package\s*\(\s*["\']([^"\']+)["\'][^)]*\)',
                ]

                found_urls = set()

                for pattern in package_patterns:
                    urls = re.findall(pattern, deps_content)
                    for url in urls:
                        if 'github.com' in url:
                            repo_key = self.extract_repo_info_from_url(url)
                            if repo_key:
                                found_urls.add(repo_key)

                dependencies = list(found_urls)

            # 패키지 이름 추출
            name_match = re.search(r'name:\s*["\']([^"\']+)["\']', content)
            package_name = name_match.group(1) if name_match else None

            return {
                'dependencies': dependencies,
                'package_name': package_name,
                'has_package_swift': True
            }

        except Exception as e:
            print(f"❌ {file_path} 파싱 오류: {e}")
            return {
                'dependencies': [],
                'package_name': None,
                'has_package_swift': False,
                'error': str(e)
            }

    def scan_directory_structure(self):
        """all_dependencies_tree 디렉토리 구조를 스캔하여 그래프 구성"""
        if not os.path.exists(self.base_dir):
            print(f"❌ 디렉토리가 존재하지 않습니다: {self.base_dir}")
            return

        print(f"🔍 {self.base_dir} 디렉토리 스캔 중...")

        # 루트 패키지들 (CSV에서 온 원본 패키지들)
        root_dirs = [d for d in os.listdir(self.base_dir)
                     if os.path.isdir(os.path.join(self.base_dir, d)) and not d.startswith('.')]

        total_packages = 0

        for root_dir in root_dirs:
            root_path = os.path.join(self.base_dir, root_dir)
            print(f"📦 루트 패키지: {root_dir}")

            # 각 루트 디렉토리 안의 모든 패키지들 스캔
            for package_dir in os.listdir(root_path):
                package_path = os.path.join(root_path, package_dir)

                if os.path.isdir(package_path):
                    # 디렉토리 이름을 repo_key로 변환 (underscore를 slash로)
                    repo_key = package_dir.replace('_', '/')

                    # Package.swift 파일 찾기
                    package_swift_path = os.path.join(package_path, "Package.swift")

                    if os.path.exists(package_swift_path):
                        print(f"  └─ 분석 중: {repo_key}")

                        # 패키지 정보 파싱
                        package_info = self.parse_package_swift(package_swift_path)
                        self.package_info[repo_key] = package_info

                        # 노드 추가
                        self.nodes.add(repo_key)
                        total_packages += 1

                        # 의존성 간선 추가
                        for dep in package_info['dependencies']:
                            self.graph[repo_key].append(dep)
                            self.reverse_graph[dep].append(repo_key)
                            self.nodes.add(dep)  # 의존성도 노드로 추가

                        if package_info['dependencies']:
                            print(
                                f"    📋 의존성 {len(package_info['dependencies'])}개: {', '.join(package_info['dependencies'][:3])}{'...' if len(package_info['dependencies']) > 3 else ''}")
                    else:
                        print(f"  ⚠️  Package.swift 없음: {repo_key}")

        print(f"✅ 총 {total_packages}개 패키지 스캔 완료")

    def calculate_graph_metrics(self):
        """그래프 메트릭 계산"""
        self.stats['total_nodes'] = len(self.nodes)
        self.stats['total_edges'] = sum(len(deps) for deps in self.graph.values())

        # 루트 패키지들 (의존성이 없는 패키지들)
        root_packages = [node for node in self.nodes if len(self.reverse_graph[node]) == 0]
        self.stats['root_packages'] = len(root_packages)

        # 리프 패키지들 (다른 패키지에 의존하지 않는 패키지들)
        leaf_packages = [node for node in self.nodes if len(self.graph[node]) == 0]
        self.stats['leaf_packages'] = len(leaf_packages)

        # 최대 깊이 계산 (BFS 사용)
        max_depth = 0
        for root in root_packages:
            depth = self.calculate_depth_from_node(root)
            max_depth = max(max_depth, depth)
        self.stats['max_depth'] = max_depth

        # 순환 의존성 탐지
        self.stats['circular_dependencies'] = self.detect_circular_dependencies()

    def calculate_depth_from_node(self, start_node):
        """특정 노드에서 시작하는 최대 깊이 계산"""
        if start_node not in self.graph:
            return 0

        visited = set()
        queue = deque([(start_node, 0)])
        max_depth = 0

        while queue:
            node, depth = queue.popleft()

            if node in visited:
                continue

            visited.add(node)
            max_depth = max(max_depth, depth)

            for dependency in self.graph[node]:
                if dependency not in visited:
                    queue.append((dependency, depth + 1))

        return max_depth

    def detect_circular_dependencies(self):
        """순환 의존성 탐지 (DFS 사용)"""
        visited = set()
        rec_stack = set()
        cycles = []

        def dfs(node, path):
            if node in rec_stack:
                # 순환 발견
                cycle_start = path.index(node)
                cycle = path[cycle_start:] + [node]
                cycles.append(cycle)
                return

            if node in visited:
                return

            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in self.graph.get(node, []):
                dfs(neighbor, path.copy())

            rec_stack.remove(node)

        for node in self.nodes:
            if node not in visited:
                dfs(node, [])

        return cycles

    def generate_graph_json(self, output_file="dependency_graph_analysis.json"):
        """완전한 그래프 JSON 생성"""

        # 노드 정보 생성
        nodes_data = []
        for node in sorted(self.nodes):
            node_info = {
                'id': node,
                'package_name': self.package_info.get(node, {}).get('package_name'),
                'has_package_swift': node in self.package_info,
                'dependencies_count': len(self.graph[node]),
                'dependents_count': len(self.reverse_graph[node]),
                'is_root': len(self.reverse_graph[node]) == 0,
                'is_leaf': len(self.graph[node]) == 0
            }
            nodes_data.append(node_info)

        # 간선 정보 생성
        edges_data = []
        edge_id = 0
        for source, targets in self.graph.items():
            for target in targets:
                edges_data.append({
                    'id': edge_id,
                    'source': source,
                    'target': target
                })
                edge_id += 1

        # 최종 JSON 구조
        graph_data = {
            'metadata': {
                'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'description': 'Swift Package Manager Dependency Graph Analysis',
                'base_directory': self.base_dir
            },
            'statistics': self.stats,
            'nodes': nodes_data,
            'edges': edges_data,
            'adjacency_list': dict(self.graph),
            'reverse_adjacency_list': dict(self.reverse_graph)
        }

        # JSON 파일로 저장
        output_path = os.path.join(self.base_dir, output_file)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(graph_data, f, indent=2, ensure_ascii=False)

        print(f"📊 그래프 분석 결과 저장: {output_path}")
        return output_path

    def generate_summary_report(self, output_file="graph_analysis_report.md"):
        """상세한 분석 리포트 생성"""
        output_path = os.path.join(self.base_dir, output_file)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("# Swift Package Dependency Graph Analysis Report\n\n")
            f.write(f"**Generated**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write("## Graph Statistics\n\n")
            f.write(f"- **Total Nodes (Repositories)**: {self.stats['total_nodes']}\n")
            f.write(f"- **Total Edges (Dependencies)**: {self.stats['total_edges']}\n")
            f.write(f"- **Root Packages**: {self.stats['root_packages']}\n")
            f.write(f"- **Leaf Packages**: {self.stats['leaf_packages']}\n")
            f.write(f"- **Maximum Dependency Depth**: {self.stats['max_depth']}\n")
            f.write(f"- **Circular Dependencies**: {len(self.stats['circular_dependencies'])}\n\n")

            # 가장 많이 의존되는 패키지들 (Top 10)
            f.write("## Most Depended Upon Packages (Top 10)\n\n")
            dependents_count = [(node, len(deps)) for node, deps in self.reverse_graph.items()]
            dependents_count.sort(key=lambda x: x[1], reverse=True)

            for i, (node, count) in enumerate(dependents_count[:10], 1):
                f.write(f"{i}. **{node}**: {count} dependents\n")

            f.write("\n## Packages with Most Dependencies (Top 10)\n\n")
            dependencies_count = [(node, len(deps)) for node, deps in self.graph.items()]
            dependencies_count.sort(key=lambda x: x[1], reverse=True)

            for i, (node, count) in enumerate(dependencies_count[:10], 1):
                f.write(f"{i}. **{node}**: {count} dependencies\n")

            # 순환 의존성 리포트
            if self.stats['circular_dependencies']:
                f.write(f"\n## Circular Dependencies ({len(self.stats['circular_dependencies'])})\n\n")
                for i, cycle in enumerate(self.stats['circular_dependencies'], 1):
                    cycle_str = " → ".join(cycle)
                    f.write(f"{i}. {cycle_str}\n")
            else:
                f.write("\n## Circular Dependencies\n\n")
                f.write("✅ No circular dependencies detected!\n")

            f.write("\n## Root Packages (No Dependencies From Others)\n\n")
            root_packages = [node for node in self.nodes if len(self.reverse_graph[node]) == 0]
            for root in sorted(root_packages):
                deps_count = len(self.graph[root])
                f.write(f"- **{root}**: {deps_count} dependencies\n")

            f.write("\n## Leaf Packages (No Dependencies To Others)\n\n")
            leaf_packages = [node for node in self.nodes if len(self.graph[node]) == 0]
            for leaf in sorted(leaf_packages):
                dependents_count = len(self.reverse_graph[leaf])
                f.write(f"- **{leaf}**: {dependents_count} dependents\n")

        print(f"📋 분석 리포트 저장: {output_path}")
        return output_path

    def run_analysis(self):
        """전체 분석 실행"""
        print("🚀 Swift Package Dependency Graph 분석 시작")
        print("=" * 60)

        # 1. 디렉토리 스캔
        self.scan_directory_structure()

        # 2. 그래프 메트릭 계산
        print("\n📊 그래프 메트릭 계산 중...")
        self.calculate_graph_metrics()

        # 3. 결과 출력
        print("\n📈 분석 결과:")
        print(f"  • 총 노드 수: {self.stats['total_nodes']}")
        print(f"  • 총 간선 수: {self.stats['total_edges']}")
        print(f"  • 루트 패키지: {self.stats['root_packages']}")
        print(f"  • 리프 패키지: {self.stats['leaf_packages']}")
        print(f"  • 최대 깊이: {self.stats['max_depth']}")
        print(f"  • 순환 의존성: {len(self.stats['circular_dependencies'])}개")

        # 4. JSON 파일 생성
        print("\n💾 결과 파일 생성 중...")
        json_path = self.generate_graph_json()
        report_path = self.generate_summary_report()

        print("\n🎉 분석 완료!")
        print(f"📁 결과 파일:")
        print(f"  • JSON: {json_path}")
        print(f"  • Report: {report_path}")


def main():
    analyzer = DependencyGraphAnalyzer()
    analyzer.run_analysis()


if __name__ == "__main__":
    main()