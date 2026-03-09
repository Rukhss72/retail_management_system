def is_admin(user) -> bool:   # admin-only
    try:
        return user.is_authenticated and user.profile.role == "admin"
    except Exception:
        return False

def is_staff_user(user) -> bool:
    try:
        return user.is_authenticated and user.profile.role == "staff"
    except Exception:
        return False

def is_staff_or_admin(user) -> bool:
    return is_admin(user) or is_staff_user(user)
