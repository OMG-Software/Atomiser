from enum import Enum
from fastapi import HTTPException, status


class Role(str, Enum):
    CONFIGURATOR = "configurator"
    ADMIN = "admin"
    MEMBER = "member"


ROLE_RANK = {
    Role.MEMBER: 1,
    Role.ADMIN: 2,
    Role.CONFIGURATOR: 3,
}


def has_role(user_role: str, required: Role) -> bool:
    try:
        role = Role(user_role)
    except ValueError:
        return False
    return ROLE_RANK.get(role, 0) >= ROLE_RANK[required]


def require_role(user, required: Role):
    if not user or not has_role(user["role"], required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this resource.",
        )
    return user
