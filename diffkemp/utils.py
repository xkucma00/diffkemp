import os
import subprocess


def get_simpll_build_dir():
    """
    Return the current SimpLL build directory as specified
    in the `SIMPLL_BUILD_DIR` environment variable.
    """
    build_dir_var = "SIMPLL_BUILD_DIR"
    if build_dir_var in os.environ:
        return os.environ[build_dir_var]
    return "build"


def get_llvm_version():
    """
    Return the current LLVM major version number.
    """
    return int(subprocess.check_output(
        ["llvm-config", "--version"]).decode().rstrip().split(".")[0])


def get_opt_command(passes, llvm_file, overwrite=True):
    """
    Return a command for running the LLVM optimizer with the given passes.
    The `passes` argument is a list of tuples `(pass_name, IR_unit)`.
    """
    opt_command = ["opt", llvm_file]
    if get_llvm_version() >= 16:
        # The new PM expects passes as "-passes=function(pass1),module(pass2)"
        passes_formatted = map(lambda p: f"{p[1]}({p[0]})", passes)
        opt_command.append("-passes=" + ",".join(passes_formatted))
    else:
        # The legacy PM expects passes as "-pass1 -pass2"
        pass_names = map(lambda p: p[0], passes)
        opt_command.extend(map(lambda pass_name: f"-{pass_name}", pass_names))
    if overwrite:
        opt_command.extend(["-S", "-o", llvm_file])
    return opt_command


class EndLineNotFound(Exception):
    """
    Error to inform that end line of function / type was not found.
    """
    pass


def get_end_line(filename, start, kind):
    """
    Get number of line where function / type ends.
    Can raise UnicodeDecodeError, EndLineError.
    """

    if kind == "function":
        terminator_list = ["}", ");"]
    elif kind == "type":
        terminator_list = ["};"]

    with open(filename, "r", encoding='utf-8') as file:
        lines = file.readlines()

        # The end of the function is detected as a line that contains
        # nothing but an ending curly bracket
        line_number = start
        line = lines[line_number - 1]
        while line.rstrip() not in terminator_list:
            line_number += 1
            if line_number > len(lines):
                raise EndLineNotFound
            line = lines[line_number - 1]
        return line_number