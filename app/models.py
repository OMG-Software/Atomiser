from pydantic import BaseModel, EmailStr, Field, field_validator


class LoginForm(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TOTPChallengeForm(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class RegisterForm(BaseModel):
    token: str = Field(min_length=16)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain a digit")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain a letter")
        return v


class InviteCreateForm(BaseModel):
    expires_hours: int = Field(default=48, ge=1, le=168)


class SiteSettingsForm(BaseModel):
    site_title: str = Field(min_length=1, max_length=120)


class UserEditForm(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    role: str = Field(pattern=r"^(admin|member)$")


class VideoMetadataForm(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    visibility: str = Field(default="site", pattern=r"^(site|private)$")
