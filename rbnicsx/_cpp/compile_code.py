# Copyright (C) 2021-2023 by the RBniCSx authors
#
# This file is part of RBniCSx.
#
# SPDX-License-Identifier: LGPL-3.0-or-later
"""Compile code in a C++ package using cppimport."""

import hashlib
import os
import sys
import types
import typing

import cppimport
import mpi4py
import mpi4py.MPI
import numpy
import petsc4py
import pybind11
import slepc4py

from rbnicsx.io import on_rank_zero

try:
    import dolfinx  # noqa: F401
except ImportError:  # pragma: no cover
    from rbnicsx._cpp.default_compiler_options_without_dolfinx import determine_default_compiler_options
else:  # pragma: no cover
    from rbnicsx._cpp.default_compiler_options_with_dolfinx import determine_default_compiler_options


def compile_code(
    comm: mpi4py.MPI.Intracomm, package_name: str, package_root: str, package_file: str,
    **kwargs: typing.Union[str, typing.List[str]]
) -> types.ModuleType:
    """Compile code in a C++ package."""
    # Merge kwargs with default compiler options
    compiler_options = dict()
    compiler_options.update(determine_default_compiler_options())
    compiler_options.update(kwargs)

    # Add include directories of python components
    assert "include_dirs" in compiler_options
    assert isinstance(compiler_options["include_dirs"], list)
    compiler_options["include_dirs"] += [
        module.get_include() for module in [mpi4py, numpy, petsc4py, slepc4py, pybind11]]

    # Add package root to include directories
    compiler_options["include_dirs"] += [package_root]

    # Determine output directory
    assert "output_dir" in compiler_options
    output_dir = compiler_options["output_dir"]

    # Write cppimport file on rank 0
    def _write_cppimport_file() -> str:
        """Write cppimport file to disk."""
        # Prepare cpp import code
        package_cppimport_code = f"""
/*
<%
setup_pybind11(cfg)
cfg["sources"] += {str(compiler_options.get("sources", []))}
cfg["dependencies"] += {str(compiler_options.get("dependencies", []))}
cfg["include_dirs"] += {str(compiler_options.get("include_dirs", []))}
cfg["compiler_args"] += {str(compiler_options.get("compiler_args", []))}
cfg["libraries"] += {str(compiler_options.get("libraries", []))}
cfg["library_dirs"] += {str(compiler_options.get("library_dirs", []))}
cfg["linker_args"] += {str(compiler_options.get("linker_args", []))}
%>
*/
"""

        # Read in content of main package file
        package_code = open(package_file).read()

        # Compute hash from package code
        package_hash = hashlib.md5(package_code.encode("utf-8")).hexdigest()
        package_name_with_hash = package_name + "_" + package_hash

        # Write to output directory
        assert isinstance(output_dir, str)
        os.makedirs(output_dir, exist_ok=True)
        open(
            os.path.join(output_dir, package_name_with_hash + ".cpp"), "w"
        ).write(
            package_cppimport_code + package_code.replace("SIGNATURE", package_name_with_hash)
        )

        # Return the library name
        return package_name_with_hash

    package_name_with_hash = on_rank_zero(comm, _write_cppimport_file)

    # Append output directory to path
    assert isinstance(output_dir, str)
    sys.path.append(output_dir)

    # Set compilers as environment variables
    mpi4py_config = mpi4py.get_config()
    if "CC" in os.environ:  # pragma: no cover
        environ_cc_changed = False
    else:  # pragma: no cover
        os.environ["CC"] = mpi4py_config["mpicc"]
        environ_cc_changed = True
    if "CXX" in os.environ:  # pragma: no cover
        environ_cxx_changed = False
    else:  # pragma: no cover
        os.environ["CXX"] = mpi4py_config["mpicxx"]
        environ_cxx_changed = True

    if comm.size > 1:
        # Compile module with cppimport on rank 0. This will fail simultaneously on all processes
        # if C++ compilation errors occur.
        def _compile_cppimport_module() -> None:
            try:
                cppimport.imp(package_name_with_hash)
            except SystemExit as e:
                raise RuntimeError(f"Compilation failed: {str(e)}.")

        on_rank_zero(comm, _compile_cppimport_module)

    # When running in serial, the next call will compile the module with cppimport.
    # When running in parallel, the module has already been compiled on rank 0 and no error as occurred.
    # Since the result of the compilation cannot be easily broadcasted from rank 0 to the other processes,
    # we call again cppimport on every rank. This should be inexpensive as it will just read the cache.
    try:
        module: types.ModuleType = cppimport.imp(package_name_with_hash)
    except SystemExit as e:
        raise RuntimeError(f"Compilation failed: {str(e)}.")

    # Clean up compilers environment variables
    if environ_cc_changed:  # pragma: no cover
        del os.environ["CC"]
    if environ_cxx_changed:  # pragma: no cover
        del os.environ["CXX"]

    # Return module generated by cppimport
    return module
