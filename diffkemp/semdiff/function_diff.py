"""Semantic difference of two functions using llreve and Z3 SMT solver."""
from diffkemp.llvm_ir.kernel_source import SourceNotFoundException
from diffkemp.simpll.simpll import run_simpll, SimpLLException
from diffkemp.semdiff.result import Result
from diffkemp.syndiff.function_syntax_diff import syntax_diff
from subprocess import Popen, PIPE
from threading import Timer
import sys


def _kill(processes):
    """ Kill each process of the list. """
    for p in processes:
        p.kill()


def _link_symbol_def(snapshot, module, symbol):
    """
    Try to find and link a missing symbol definition inside the given snapshot.
    Look inside the kernel directory if no definition is found inside the
    primary snapshot directory. The search is skipped when a source file
    change after the snapshot creation time is detected.
    :param snapshot: Snapshot where the definition should be.
    :param module: Module which requires the  missing definition.
    :param symbol: Symbol with a missing definition to look for.
    :return: True if the symbol has been successfully linked, False otherwise.
    """
    new_mod = None
    result = False
    time = snapshot.created_time.timestamp() if snapshot.created_time else None

    try:
        new_mod = snapshot.snapshot_source.get_module_for_symbol(symbol, time)
    except SourceNotFoundException:
        if snapshot.kernel_source:
            try:
                new_mod = snapshot.kernel_source.get_module_for_symbol(symbol,
                                                                       time)
            except SourceNotFoundException:
                pass

    if new_mod:
        if module.link_modules([new_mod]):
            result = True
        new_mod.clean_module()

    return result


def _run_llreve_z3(first, second, funFirst, funSecond, coupled, timeout,
                   verbose):
    """
    Run the comparison of semantics of two functions using the llreve tool and
    the Z3 SMT solver. The llreve tool takes compared functions in LLVM IR and
    generates a formula in first-order predicate logic. The formula is then
    solved using the Z3 solver. If it is unsatisfiable, the compared functions
    are semantically the same, otherwise, they are different.

    The generated formula is in the theory of bitvectors.

    :param first: File with the first LLVM module
    :param second: File with the second LLVM module
    :param funFirst: Function from the first module to be compared
    :param funSecond: Function from the second module to be compared
    :param coupled: List of coupled functions (functions that are supposed to
                    correspond to each other in both modules). These are needed
                    for functions not having definintions.
    :param timeout: Timeout for the analysis in seconds
    :param verbose: Verbosity option
    """

    stderr = None
    if not verbose:
        stderr = open('/dev/null', 'w')

    # Commands for running llreve and Z3 (output of llreve is piped into Z3)
    command = ["build/llreve/reve/reve/llreve",
               first, second,
               "--fun=" + funFirst + "," + funSecond,
               "-muz", "--ir-input", "--bitvect", "--infer-marks",
               "--disable-auto-coupling"]
    for c in coupled:
        command.append("--couple-functions={},{}".format(c[0], c[1]))

    if verbose:
        sys.stderr.write(" ".join(command) + "\n")

    llreve_process = Popen(command, stdout=PIPE, stderr=stderr)

    z3_process = Popen(["z3", "fixedpoint.engine=duality", "-in"],
                       stdin=llreve_process.stdout,
                       stdout=PIPE, stderr=stderr)

    # Set timeout for both tools
    timer = Timer(timeout, _kill, [[llreve_process, z3_process]])
    try:
        timer.start()

        z3_process.wait()
        result_kind = Result.Kind.ERROR
        # Processing the output
        for line in z3_process.stdout:
            line = line.strip()
            if line == b"sat":
                result_kind = Result.Kind.NOT_EQUAL
            elif line == b"unsat":
                result_kind = Result.Kind.EQUAL
            elif line == b"unknown":
                result_kind = Result.Kind.UNKNOWN

        if z3_process.returncode != 0:
            result_kind = Result.Kind.ERROR
    finally:
        if not timer.is_alive():
            result_kind = Result.Kind.TIMEOUT
        timer.cancel()

    return Result(result_kind, first, second)


def functions_semdiff(first, second, fun_first, fun_second, config):
    """
    Compare two functions for semantic equality.

    Functions are compared under various assumptions, each having some
    'assumption level'. The higher the level is, the more strong assumption has
    been made. Level 0 indicates no assumption. These levels are determined
    from differences of coupled functions that are set as a parameter of the
    analysis. The analysis tries all assumption levels in increasing manner
    until functions are proven to be equal or no assumptions remain.

    :param first: File with the first LLVM module
    :param second: File with the second LLVM module
    :param fun_first: Function from the first module to be compared
    :param fun_second: Function from the second module to be compared
    :param config: Configuration.
    """
    if fun_first == fun_second:
        fun_str = fun_first
    else:
        fun_str = fun_second
    sys.stdout.write("      Semantic diff of {}".format(fun_str))
    sys.stdout.write("...")
    sys.stdout.flush()

    # Run the actual analysis
    if config.semdiff_tool == "llreve":
        called_first = first.get_functions_called_by(fun_first)
        called_second = second.get_functions_called_by(fun_second)
        called_couplings = [(f, s) for f in called_first for s in called_second
                            if f == s]
        result = _run_llreve_z3(first.llvm, second.llvm, fun_first, fun_second,
                                called_couplings, config.timeout,
                                config.verbosity)
        first.clean_module()
        second.clean_module()
        return result


def functions_diff(mod_first, mod_second,
                   fun_first, fun_second,
                   glob_var, config,
                   cache=None, simpll_cache=None):
    """
    Compare two functions for equality.

    First, functions are simplified and compared for syntactic equality using
    the SimpLL tool. If they are not syntactically equal, SimpLL prints a list
    of functions that the syntactic equality depends on. These are then
    compared for semantic equality.
    :param mod_first: First LLVM module
    :param mod_second: Second LLVM module
    :param fun_first: Function from the first module to be compared
    :param fun_second: Function from the second module to be compared
    :param glob_var: Global variable whose effect on the functions to compare
    :param config: Configuration
    """
    result = Result(Result.Kind.NONE, fun_first, fun_second)
    graph = None
    try:
        if config.verbosity:
            if fun_first == fun_second:
                fun_str = fun_first
            else:
                fun_str = "{} and {}".format(fun_first, fun_second)
            print("Syntactic diff of {} (in {})".format(fun_str,
                                                        mod_first.llvm))

        simplify = True
        while simplify:
            simplify = False
            if (cache and fun_first in cache.vertices and
                    (cache.vertices[fun_first].result !=
                     Result.Kind.ASSUMED_EQUAL)):
                first_simpl = ""
                second_simpl = ""
                graph = cache
                missing_defs = []
            else:
                # Simplify modules and get the output graph.
                if simpll_cache:
                    simpll_cache.rollback()
                first_simpl, second_simpl, graph, missing_defs = \
                    run_simpll(first=mod_first.llvm, second=mod_second.llvm,
                               fun_first=fun_first, fun_second=fun_second,
                               var=glob_var.name if glob_var else None,
                               suffix=glob_var.name if glob_var else "simpl",
                               cache_dir=simpll_cache.directory
                               if simpll_cache else None,
                               control_flow_only=config.control_flow_only,
                               print_asm_diffs=config.print_asm_diffs,
                               verbose=config.verbosity)
                # Add the newly received results to the ignored functions file.
                # Note: there won't be any duplicates, since all functions
                # that were in the cache before will be marked as unknown.
                if simpll_cache:
                    simpll_cache.update([v for v in graph.vertices.values()
                                         if v.result not in
                                         [Result.Kind.UNKNOWN,
                                          Result.Kind.ASSUMED_EQUAL]])

                if cache:
                    # Note: "graph" is here the partial result graph, i.e. can
                    # contain unknown results that are known in the cache.
                    # Hence it is necessary to absorb the graph into the cache,
                    # not vice versa.
                    cache.absorb_graph(graph)
                    graph = cache
            objects_to_compare, syndiff_bodies_left, syndiff_bodies_right = \
                graph.graph_to_fun_pair_list(fun_first, fun_second)
            funs_to_compare = list([o for o in objects_to_compare
                                    if o[0].diff_kind == "function"])
            if funs_to_compare and missing_defs:
                # If there are missing function definitions, try to find their
                # implementation, link them to the current modules, and rerun
                # the simplification.
                for fun_pair in missing_defs:
                    if "first" in fun_pair:
                        if _link_symbol_def(config.snapshot_first, mod_first,
                                            fun_pair["first"]):
                            simplify = True

                    if "second" in fun_pair:
                        if _link_symbol_def(config.snapshot_second, mod_second,
                                            fun_pair["second"]):
                            simplify = True

        if simpll_cache:
            simpll_cache.reset_rollback_cache()

        mod_first.restore_unlinked_llvm()
        mod_second.restore_unlinked_llvm()

        if not objects_to_compare:
            result.kind = Result.Kind.EQUAL_SYNTAX
        else:
            # If the functions are not syntactically equal, objects_to_compare
            # contains a list of functions and macros that are different.
            for fun_pair in objects_to_compare:
                if (not fun_pair[0].diff_kind == "function" and
                        config.semdiff_tool is not None):
                    # If a semantic diff tool is set, use it for further
                    # comparison of non-equal functions
                    fun_result = functions_semdiff(first_simpl, second_simpl,
                                                   fun_pair[0].name,
                                                   fun_pair[1].name,
                                                   config)
                else:
                    fun_result = Result(fun_pair[2], fun_first, fun_second)
                fun_result.first = fun_pair[0]
                fun_result.second = fun_pair[1]
                if fun_result.kind == Result.Kind.NOT_EQUAL:
                    if fun_result.first.diff_kind in ["function", "type"]:
                        # Get the syntactic diff of functions or types
                        fun_result.diff = syntax_diff(
                            fun_result.first.filename,
                            fun_result.second.filename,
                            fun_result.first.name,
                            fun_result.first.diff_kind,
                            fun_pair[0].line,
                            fun_pair[1].line)
                    elif fun_result.first.diff_kind == "syntactic":
                        # Find the syntax differences and append the left and
                        # right value to create the resulting diff
                        fun_result.diff = "  {}\n\n  {}\n".format(
                            syndiff_bodies_left[fun_result.first.name],
                            syndiff_bodies_right[fun_result.second.name])
                    else:
                        sys.stderr.write(
                            "warning: unknown diff kind: {}\n".format(
                                fun_result.first.diff_kind))
                        fun_result.diff = "unknown\n"
                result.add_inner(fun_result)
        if config.verbosity:
            print("  {}".format(result))
    except ValueError:
        result.kind = Result.Kind.ERROR
    except SimpLLException as e:
        if config.verbosity:
            print(e)
        result.kind = Result.Kind.ERROR
    result.cache = graph if graph else cache
    return result
