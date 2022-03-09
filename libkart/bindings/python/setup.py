import os
from pathlib import Path

from setuptools import Extension, setup
from Cython.Build import cythonize

# TODO: set this in build env
LIBKART_SRC_PREFIX = Path(
    os.environ.get(
        "LIBKART_SRC_PREFIX",
        Path(__file__).resolve().parent.parent.parent,
    )
)
LIBKART_BUILD_DIR = LIBKART_SRC_PREFIX / "build"

setup(
    name="libkart",
    ext_modules=cythonize(
        Extension(
            "libkart",
            sources=["libkart.pyx"],
            language="c++",
            extra_compile_args=["--std=c++20", "-O0", "-g"],
            libraries=["kart"],
            # extra_link_args=["-rpath", str(LIBKART_BUILD_DIR)],
        ),
        compiler_directives={"language_level": "3"},
    ),
)