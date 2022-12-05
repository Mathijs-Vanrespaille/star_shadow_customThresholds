"""STAR SHADOW

Code written by: Luc IJspeert
"""

from setuptools import setup


# package version
MAJOR = 1
MINOR = 0
ATTR = '0'
# full acronym
ACRONYM = ('Satellite Time-series Analysis Routine using Sinusoids and Harmonics Automatedly '
           'for Double stars with Occultations and Waves')

setup(name="star_shadow",
      version=f'{MAJOR}.{MINOR}.{ATTR}',
      author='Luc IJspeert',
      license='GNU General Public License v3.0',
      description=ACRONYM,
      long_description=open('README.md').read(),
      packages=['star_shadow'],
      package_dir={'star_shadow': 'star_shadow'},
      package_data={'star_shadow': ['star_shadow/data/tess_sectors.dat', 'star_shadow/data/mpl_stylesheet.dat']},
      include_package_data=True,
      install_requires=['numpy', 'scipy', 'numba', 'astropy', 'ellc', 'arviz', 'h5py', 'matplotlib', 'corner'])
