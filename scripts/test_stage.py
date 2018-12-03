#!/usr/bin/env python3

"""
This file contains the entry point of the script that performs the Oozie testing stage.
"""

import argparse

from pathlib import Path

import logging
import pickle
import sys
import traceback

from typing import Any, List

import dbd_build
import output
import oozie_testing.examples

# pylint: disable=useless-import-alias

import oozie_testing.inside_container.report as report

# pylint: enable=useless-import-alias

import test_env

def write_to_file(text: str, path: Path) -> None:
    """
    Writes a string to a file, making sure that the parents of the file path exist, creating them if needed.

    Args:
        text: The string that should be written to the file.
        path: The path of the file that should be written.

    """

    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("w") as file:
        file.write(text)

def get_argument_parser() -> argparse.ArgumentParser:
    """
    Builds and returns an argument parser for the script entry point.

    Returns:
        An argument parser for the script entry point.

    """

    parser = argparse.ArgumentParser(description="Generates the dbd configuration directories and runs "
                                     + "the Oozie examples within the resulting dockerised clusters.")
    parser.add_argument("configurations_dir", help="The directory in which the BuildConfiguration files are located.")
    parser.add_argument("output_dir", help="The directory in which the output of the build will be generated.")
    parser.add_argument("-c", "--configurations", nargs="*",
                        help="A list of filenames relative to `configurations_dir`. "
                        + "If provided, only those `BuildConfiguration`s will be built.")
    parser.add_argument("-w", "--whitelist",
                        metavar="WHITELIST", nargs="*",
                        help="Only run the whitelisted examples. Otherwise, all detected examples are run.")
    parser.add_argument("-b", "--blacklist",
                        metavar="BLACKLIST", nargs="*",
                        help="Do not run the blacklisted examples.")
    parser.add_argument("-v", "--validate", nargs="*",
                        help="A list of fluent examples that should only be validated, not run.")
    parser.add_argument("-t", "--timeout", type=int, help="The timeout after which running examples are killed.")

    return parser

class ReportUnpickler(pickle.Unpickler):
    """
    A custom `pickle.Unpickler` that is needed because the object we would like to unpickle is generated
    inside a docker container where the import path of the class of the object is different than here.

    """

    def find_class(self, module: str, name: str) -> Any:
        if module == "report":
            return report.__dict__[name]

        return super().find_class(module, name)

def copy_logs(oozieserver_name: str,
              nodemanager_name: str,
              current_report_dir: Path,
              logfile: str,
              report_records_file: str) -> None:
    """
    Copies the logfile and the file containing the test results, as well as
    the Oozie and Yarn logs from the docker containers to the local file system.

    Args:
        oozieserver_name: The name of the Oozie server container.
        nodemanager_name: The name of the node manager container.
        current_report_dir: The path on the local file system where the logs and the report should be placed.
        logfile: The path to the logfile generated by the example running script inside the Oozie server.
        report_records_file: The path to the report records file (the file containing the test results)
            generated by the example running script inside the Oozie server.

    """

    test_env.copy_logfile_and_report_records(oozieserver_name, logfile, report_records_file, current_report_dir)
    test_env.copy_oozie_logs(oozieserver_name, current_report_dir / "oozieserver")
    test_env.copy_yarn_logs(nodemanager_name, current_report_dir / "nodemanager")

def write_report(build_config_name: str,
                 current_report_dir: Path,
                 report_records_file: str) -> None:
    """
    Generates and writes a junit style xml report file.

    Args:
        current_report_dir: The directory in which the report file will be written.
        report_records_file: The name of the file containing the results of the test.
            This file will be pickled to retrieve the corresponding object.
    """

    report_records: List[report.ReportRecord]
    local_report_records_file = current_report_dir / report_records_file
    with (local_report_records_file).open("rb") as file:
        report_records = ReportUnpickler(file).load()

        local_report_records_file.unlink()

        xml_report = output.generate_report(build_config_name, report_records, current_report_dir)
        xml_report_file = current_report_dir / "report_examples.xml"
        xml_report.write(str(xml_report_file))

def perform_testing(args: argparse.Namespace,
                    reports_dir: Path,
                    build_config_name: str,
                    timeout: int) -> int:
    """
    In a running dockerised cluster, performs the initialisation of the environment,
    runs the tests, collects the logs and generates the test report.

    Args:
        args: The arguments parsed from the command line.
        reports_dir: The directory where the reports of the various `BuildConfiguration`s' test results
            should be located. The reports for the individual `BuildConfigurations` will be placed in
            subdirectories with the name of the `BuildConfiguration`.
        build_config_name: The name of the current `BuildConfiguration`.
        timeout: The timeout after which running examples are killed.

    Returns:
        The exit code of the process running the example tests inside the Oozie docker container.
        This value is 1 if any tests failed.

    """

    oozieserver = test_env.get_oozieserver()
    inside_container = Path(oozie_testing.inside_container.__file__).parent.expanduser().resolve()
    test_env.setup_testing_env_in_container(oozieserver, inside_container)

    examples_logfile = "example_runner.log"
    examples_report_records_file = "report_records.pickle"

    exit_code_examples = oozie_testing.examples.run_oozie_examples_with_dbd(oozieserver,
                                                                            examples_logfile,
                                                                            examples_report_records_file,
                                                                            args.whitelist,
                                                                            args.blacklist,
                                                                            args.validate,
                                                                            timeout)

    current_report_dir = reports_dir / build_config_name

    nodemanager = test_env.get_nodemanager()

    copy_logs(oozieserver.name, nodemanager.name, current_report_dir, examples_logfile, examples_report_records_file)

    write_report(build_config_name, current_report_dir, examples_report_records_file)

    return exit_code_examples

def start_cluster_and_perform_testing(args: argparse.Namespace,
                                      reports_dir: Path,
                                      build_config_dir: Path,
                                      timeout: int) -> int:
    """
    Starts the dockerised cluster, performs initialisation and testing,
    collects the logs, generates the test report and stops the cluster.

    Args:
        args: The arguments parsed from the command line.
        reports_dir: The directory where the reports of the various `BuildConfiguration`s' test results
            should be located. The reports for the individual `BuildConfigurations` will be placed in
            subdirectories with the name of the `BuildConfiguration`.
        build_config_dir: The directory where the `BuildConfiguration`
            is built and where the docker-compose file is located.
        timeout: The timeout after which running examples are killed.

    Returns:
        The exit code of the process running the example tests inside the Oozie docker container.
        This value is 1 if any tests failed and 2 if an exception occurred - 0 otherwise.

    """

    exit_code: int

    try:
        test_env.docker_compose_up(build_config_dir)
        exit_code = perform_testing(args, reports_dir, build_config_dir.name, timeout)

    # We catch all exceptions to be able to continue with other BuildConfigurations if there are any.
    # pylint: disable=broad-except
    except Exception as ex:
        print("An exception has occured: {}.".format(ex))
        traceback.print_exc(file=sys.stdout)

        exit_code = 2
    finally:
        test_env.docker_compose_down(build_config_dir)

    return exit_code

def main() -> None:
    """
    The entry point of the script.
    """

    args = get_argument_parser().parse_args()

    configurations_dir = Path(args.configurations_dir)
    output_dir = Path(args.output_dir)
    reports_dir = Path("testing/reports")
    dbd_path = Path("testing/dbd/run_dbd.py")
    cache_dir = Path("./dbd_cache")
    timeout = args.timeout if args.timeout is not None else 180

    dbd_build.build_configs_with_dbd(configurations_dir, args.configurations, output_dir, dbd_path, cache_dir)

    build_config_dirs = output_dir.expanduser().resolve().iterdir()

    test_exit_codes = map(
        lambda build_config_dir: start_cluster_and_perform_testing(args, reports_dir, build_config_dir, timeout),
        build_config_dirs)

    max_exit_code = max(test_exit_codes)

    if max_exit_code != 0:
        sys.exit(max_exit_code)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
