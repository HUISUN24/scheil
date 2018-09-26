Releasing scheil
==========================

When releasing a new version of scheil:

1. ``git pull`` to make sure you haven't missed any last-minute commits. **After this point, nothing else is making it into this version.**
#. Ensure that all tests pass locally on develop.
#. Regenerate the API documentation with ``sphinx-apidoc -f -o docs/api/ scheil/ -H 'API Documentation'``.
#. Update the ``version`` and ``release`` variables of ``docs/conf.py`` to the new version
#. Resolve differences and commit the updated API documentation. 
#. ``git push`` and verify all tests pass on all CI services.
#. Generate a list of commits since the last version with ``git --no-pager log --oneline --no-decorate 0.1^..origin/master``
   Replace ``0.1`` with the tag of the last public version.
#. Condense the change list into something user-readable. Update and commit CHANGES.rst with the release date.``
#. ``git tag 0.2 master -m "0.2"`` Replace ``0.2`` with the new version. 
   ``git show 0.2`` to ensure the correct commit was tagged
   ``git push origin master --tags``
#. The new version is tagged in the repository. Now the public package must be built and distributed.

Uploading to PyPI
-----------------

1. ``rm -R dist/*`` on Linux/OSX or ``del dist/*`` on Windows
2. With the commit checked out which was tagged with the new version:
   ``python setup.py sdist``

   **Make sure that the script correctly detected the new version exactly and not a dirty / revised state of the repo.**

   Assuming a correctly configured .pypirc:

   ``twine upload -r pypi -u bocklund dist/*``

Updating the conda-forge feedstock
----------------------------------

1. Get the sha-256 hash of the tarball via ``openssl sha256 dist/scheil-0.2.tar.gz`` or at `pypi.org <https://pypi.org/project/scheil>`_.
2. Fork the `conda forge scheil feedstock <https://github.com/conda-forge/scheil/>`_.
3. Update the version number and hash in the recipe
4. Open a PR and merge once the tests complete and pass
