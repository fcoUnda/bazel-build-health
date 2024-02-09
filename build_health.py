# Usage: python experimental/users/unda/scripts/build_health.py <bazel target>

import argparse
from collections import defaultdict
import json
import sys
import os
import re
import subprocess
import time


def attempt_build(target_pattern, bep_path):
    """Runs Bazel build and generates the BEP file."""
    subprocess.run(
        [
            "bazel",
            f"--output_base={sys.path[0]}/output_base",
            "build",
            target_pattern,
            f"--build_event_json_file={bep_path}",
            "--keep_going",
            "--ui_event_filters=-INFO,-ERROR,-DEBUG,-WARNING",
        ],
        check=False,  # Raise an exception if Bazel fails
    )


class PackageTreeNode:

    def __init__(self, package: str, depth: int):
        self.package: str = package
        self.depth: int = depth
        self.children: list[PackageTreeNode] = []
        self.package_num_successes = 0
        self.package_num_targets = 0
        self.subtree_num_successes = 0
        self.subtree_num_targets = 0


def insert_node_into_tree(root: PackageTreeNode, node: PackageTreeNode):
    found_longer_suffix = False
    for child in root.children:
        if node.package.startswith(child.package):
            insert_node_into_tree(child, node)
            found_longer_suffix = True
            break
    if not found_longer_suffix:
        node.depth = root.depth + 1
        root.children.append(node)


def insert_node_into_forest(roots: list[PackageTreeNode], node: PackageTreeNode):
    found_suffix = False
    for root in roots:
        if node.package.startswith(root.package):
            insert_node_into_tree(root, node)
            found_suffix = True
            break
    if not found_suffix:
        node.depth = 0
        roots.append(node)
    return roots


def build_package_forest(package_lst: list[str]) -> list[PackageTreeNode]:
    result = []
    for package in package_lst:
        n = PackageTreeNode(package, 0)
        result = insert_node_into_forest(result, n)
    return result


def get_package(target):
    match = re.search(r"^(.*?):", target)
    if match:
        return match.group(1)
    else:
        raise RuntimeError(f"Error parsing package for: {target}.")


def compute_counts(node, targets_from_package, outcomes):
    subtree_number_of_targets = 0
    subtree_number_of_successes = 0
    for n in node.children:
        compute_counts(n, targets_from_package, outcomes)
        subtree_number_of_targets += n.subtree_num_targets
        subtree_number_of_successes += n.subtree_num_successes

    targets = targets_from_package[node.package]
    node.package_num_targets = len(targets)
    node.package_num_successes = sum(outcomes[t] == "success" for t in targets)
    node.subtree_num_targets = subtree_number_of_targets + node.package_num_targets
    node.subtree_num_successes = (
        subtree_number_of_successes + node.package_num_successes
    )


def print_forest(r, package_to_target, outcome_of, print_individual_targets, expand):
    queue = [r]
    while queue:
        node, queue = queue[0], queue[1:]
        subtree_success_fraction = node.subtree_num_successes / node.subtree_num_targets
        indentation = "|" * (node.depth) + "├"
        if subtree_success_fraction < 1 or expand:
            package_success_fraction = (
                node.package_num_successes / node.package_num_targets
            )
            print(
                f"{indentation}Package {node.package}:all build percentage:"
                f" {package_success_fraction:.1%} ({node.package_num_successes}/{node.package_num_targets})"
            )
            if expand and print_individual_targets:
                for t in package_to_target[node.package]:
                    if (outcome_of[t] == "success" and expand) or outcome_of[
                        t
                    ] != "success":
                        print(f" {indentation}Target {t} : {outcome_of[t]}")
            queue = sorted(node.children, key=lambda x: x.package) + queue


def read_build_event_protocol(path):
    """Reads the BEP file and prints basic build event information."""
    outcomes = {}
    with open(path, "rb") as f:
        for line in f:
            bep_contents = json.loads(line)
            for k in bep_contents["id"].keys():
                if k == "targetConfigured":
                    label = bep_contents["id"]["targetConfigured"]["label"]
                    outcomes[label] = None
                elif k == "targetCompleted":
                    label = bep_contents["id"]["targetCompleted"]["label"]
                    if label not in outcomes:
                        raise RuntimeError(f"Haven't seen this target before: {label}")
                    if "aborted" in bep_contents:
                        outcomes[label] = "aborted"
                    elif "completed" in bep_contents:
                        if (
                            "success" in bep_contents["completed"]
                            and bep_contents["completed"]["success"] == True
                        ):
                            outcomes[label] = "success"
                        elif "failureDetail" in bep_contents["completed"]:
                            outcomes[label] = bep_contents["completed"][
                                "failureDetail"
                            ]["message"]
                        else:
                            outcomes[label] = str(bep_contents["completed"])
                    else:
                        outcomes[label] = str(bep_contents)
    return outcomes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Calculate success percentage of Bazel builds for packages. If called"
            " for a single package, it will show targets as well."
        )
    )
    parser.add_argument("target", help="The Bazel query to extract targets from.")
    parser.add_argument(
        "--expand",
        default=False,
        help="If set, it will unfold packages that have completely built.",
    )
    bep_file_path = "build_events.json"
    args = parser.parse_args()

    attempt_build(args.target, bep_file_path, args.blaze)

    # Small delay to ensure the BEP file is fully written
    time.sleep(1)

    # capture the outcome of all targets
    outcome_of = read_build_event_protocol(bep_file_path)

    # build package to targets dictionary
    package_to_targets = defaultdict(list)
    for target in outcome_of:
        package_to_targets[get_package(target)].append(target)

    # do we want to print invidual targets
    print_individual_targets = len(package_to_targets.keys()) <= 1

    # build package tree structure
    all_roots = build_package_forest(sorted(package_to_targets.keys()))

    # compute subtree and individual success/total counts.
    for r in all_roots:
        compute_counts(r, package_to_targets, outcome_of)

    # print tree with build success aggregation
    print()
    for r in sorted(all_roots, key=lambda x: x.package):
        print_forest(
            r, package_to_targets, outcome_of, print_individual_targets, args.expand
        )

    overall_percentage = (
        sum(o == "success" for o in outcome_of.values()) / len(outcome_of) * 100
    )
    print()
    print(f"Overall build percent: {overall_percentage:.2f}%")
