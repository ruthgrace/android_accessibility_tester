from setuptools import setup, find_packages

setup(
    name="android-accessibility-tester",
    version="2.1.1",
    description="Python library for testing Android apps with accessibility features using ADB and Claude LLM",
    author="Your Name",
    py_modules=["android_accessibility_tester"],
    install_requires=[
        "anthropic>=0.39.0",
        "pytest>=7.0.0",
        "Pillow>=9.0.0",
    ],
    python_requires=">=3.7",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Testing",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)
