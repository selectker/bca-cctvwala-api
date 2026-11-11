"""The database connection verifies the server certificate (libpq's verify-full), it doesn't just encrypt."""
import ssl

import pytest

from app import db
from app.config import settings

# A throw-away self-signed CA, only used to check that a CA file is loaded and enforced.
CA_PEM = """-----BEGIN CERTIFICATE-----
MIIDDzCCAfegAwIBAgIUIbDqPPX5zBwIM7LovDCxBrGgOxAwDQYJKoZIhvcNAQEL
BQAwFjEUMBIGA1UEAwwLVGVzdCBSRFMgQ0EwIBcNMjYxMDE0MTkxMTQ3WhgPMjEy
NjA5MjAxOTExNDdaMBYxFDASBgNVBAMMC1Rlc3QgUkRTIENBMIIBIjANBgkqhkiG
9w0BAQEFAAOCAQ8AMIIBCgKCAQEA3w35XAwVuRob3yrxemHctAmXk2/rdgVcu3Ig
A3ICs37ZrBtxOKFuUjYjSwhSTJaE1qKL2IaEOMCpjniDdmbHjIFsODkTzxYQ/Qkd
n2MV1YbptM37D/Vf0aO/vSvirr9oR8JuVv8KoUA9Xe9re9HltK8UmW4hCJatl4eG
MGLuB9gUq/EthfVHPAgxm5g9oDv+tXeLu9vGzaNKKPQjvp2LLGKSe9Ab8BVckFyg
VnDUIHlUkNvJMnyMTR6VV/AOfH/GLpqyoeYHm/npf3Gj5FS0tEp5pMxltUfZfFcK
mIk9nTieR2h6CiZz/VFOFc6lesRV+n/13he+7PmxNrA6CEUX0wIDAQABo1MwUTAd
BgNVHQ4EFgQUUhHcb7HDnlOAmkbRL30hF04ACP0wHwYDVR0jBBgwFoAUUhHcb7HD
nlOAmkbRL30hF04ACP0wDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG9w0BAQsFAAOC
AQEAcXef9lyTBoQ86Gl1fX3rSy9sKq46uZfQ21ZeY+/wV9cK5ObbFT+eIG3E+IvM
spaIcGg+E+05LsZX3fmCwpby1xM0fbdVfcYggCvDl7QmEMcMOpp0c9ssZTQGEyIj
r/49r3F+h5vM61sY4Z/Yi75r070O2Jn9rWajK9h7LwECDJK4D/81uksWkrVYFHzB
PQ0gqluNRf9K1AxhGSEWhQaQRlyVkSmTAaygz4uxGhidv3TXj3SHSS3RhjMWrdKJ
o7I0U87n49wlU6tQmEPPUuVduiQtgFQr2nq8U8dgFfQrjgbLJcJ9u9PJ1pcMn39f
OYWLJo1AOIHLcG7V5Ih9W8xYYA==
-----END CERTIFICATE-----
"""


def _configure(monkeypatch, *, ssl_on=True, ca_file=""):
    monkeypatch.setattr(settings, "db_ssl", ssl_on)
    monkeypatch.setattr(settings, "db_ssl_ca_file", ca_file)


def test_verified_tls_with_the_given_ca(monkeypatch, tmp_path):
    ca = tmp_path / "rds-ca.pem"
    ca.write_text(CA_PEM)
    _configure(monkeypatch, ca_file=str(ca))
    ctx = db.build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    # ONLY that CA is trusted (not the system store), so a certificate from any other authority is refused.
    assert [c["subject"][0][0][1] for c in ctx.get_ca_certs()] == ["Test RDS CA"]


def test_tls_without_a_ca_file_refuses_to_start(monkeypatch, tmp_path):
    _configure(monkeypatch, ca_file="")
    with pytest.raises(RuntimeError, match="DB_SSL_CA_FILE"):
        db.build_ssl_context()
    _configure(monkeypatch, ca_file=str(tmp_path / "missing.pem"))
    with pytest.raises(RuntimeError, match="does not exist"):
        db.build_ssl_context()
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate")
    _configure(monkeypatch, ca_file=str(junk))
    with pytest.raises(RuntimeError, match="could not be loaded"):
        db.build_ssl_context()


def test_tls_can_only_be_switched_off_explicitly(monkeypatch):
    _configure(monkeypatch, ssl_on=False)
    assert db.build_ssl_context() is None
