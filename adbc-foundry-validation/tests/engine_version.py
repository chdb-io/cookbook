# The engine version the Foundry validation quirks pin (major.minor). This is
# the single place to bump on an upstream baseline sync; the tripwire test in
# tests/test_adbc_driver.py fails in every CI matrix until it matches the
# built engine, and the validation suite must be rerun to re-confirm the
# capability matrix on the new baseline.
SHORT_VERSION = "26.5"
