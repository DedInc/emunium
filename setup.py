from setuptools import setup, find_packages

with open('README.md', 'r', encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='emunium',
    version='1.0.0',
    author='Maehdakvan',
    author_email='visitanimation@google.com',
    description='A Python module for automating interactions to mimic human behavior in browsers when using Selenium. Provides utilities to programmatically move the mouse cursor, click on page elements, type text, and scroll as if performed by a human user.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/DedInc/emunium',
    project_urls={
        'Bug Tracker': 'https://github.com/DedInc/emunium/issues',
    },
    classifiers=[
        'Programming Language :: Python :: 3',
        'License :: OSI Approved :: MIT License',
        'Operating System :: OS Independent',
    ],
    packages=find_packages(),
    include_package_data = True,
    install_requires = ['pyautogui', 'keyboard'],
    python_requires='>=3.6'
)