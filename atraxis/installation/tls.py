"""Validate supplied bytes, never open a user-selected certificate/key path."""
from datetime import datetime, timezone
from ipaddress import ip_address
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID

from atraxis.installation.validation import InstallError, MAX_TLS_BYTES, require

MAX_CHAIN_CERTIFICATES = 8
MIN_RSA_BITS = 2048
MIN_EC_BITS = 256


def validate_tls(certificate, private_key, server_name):
    try:
        for value in (certificate, private_key):
            require(type(value) is bytes and 0 < len(value) <= MAX_TLS_BYTES)
        certificates = x509.load_pem_x509_certificates(certificate)
        require(1 <= len(certificates) <= MAX_CHAIN_CERTIFICATES)
        # Reject appended directives, extra keys and arbitrary pre/postamble.
        require(b''.join(c.public_bytes(serialization.Encoding.PEM) for c in certificates) == certificate)
        key = serialization.load_pem_private_key(private_key, password=None)
        require(private_key == key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        require((isinstance(key, rsa.RSAPrivateKey) and key.key_size >= MIN_RSA_BITS)
                or (isinstance(key, ec.EllipticCurvePrivateKey) and key.key_size >= MIN_EC_BITS))
        public = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        require(public(key.public_key()) == public(certificates[0].public_key()))
        now = datetime.now(timezone.utc)
        for cert in certificates:
            require(cert.not_valid_before_utc <= now < cert.not_valid_after_utc)
        leaf = certificates[0]
        require(not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        try:
            ip = ip_address(server_name)
        except ValueError:
            require(server_name in san.get_values_for_type(x509.DNSName))
        else:
            require(ip in san.get_values_for_type(x509.IPAddress))
        try:
            require(ExtendedKeyUsageOID.SERVER_AUTH in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
        except x509.ExtensionNotFound:
            pass
        for child, issuer in zip(certificates, certificates[1:]):
            require(issuer.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            child.verify_directly_issued_by(issuer)
    except Exception:
        raise InstallError('invalid_tls', 'tls') from None
