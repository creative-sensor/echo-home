import setuptools
import os

print("PACKAGES: " + setuptools.find_packages(where="src"))
setuptools.setup(
    name = os.environ['PACKAGE_NAME'],
    version = os.environ['PACKAGE_VERSION'],
    author = os.environ['AUTHOR_NAME'],
    author_email = os.environ["AUTHOR_EMAIL"],
    description = os.environ['PACKAGE_DESCRIPTION'],
    classifiers = [
        "Programming Language :: Python :: 3",
    ],
    package_dir = {"": "src"},
    packages = setuptools.find_packages(where="src"),
    python_requires = ">=3.6",
)
