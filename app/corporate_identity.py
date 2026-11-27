import jwt


class CorporateIdentity:
    def __init__(self, settings):
        self.issuer = settings.oidc_issuer
        self.audience = settings.oidc_client_id
        self.domain = settings.oidc_email_domain.casefold()
        self.jwks = jwt.PyJWKClient(settings.oidc_jwks, lifespan=300, timeout=3)

    def verify(self, token: str) -> str | None:
        if len(token) > 8192 or token.count(".") != 2:
            return None
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                return None
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["iss", "sub", "aud", "iat", "exp", "email", "email_verified"]},
            )
            audiences = claims["aud"]
            if (isinstance(audiences, list) and len(audiences) > 1) or "azp" in claims:
                if claims.get("azp") != self.audience:
                    return None
            if not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255:
                return None
            email = claims["email"]
            if claims["email_verified"] is not True or not isinstance(email, str):
                return None
            local, separator, domain = email.rpartition("@")
            if not separator or not local or "@" in local or domain.casefold() != self.domain:
                return None
            return claims["sub"]
        except (jwt.PyJWTError, ValueError, TypeError, OSError):
            return None
