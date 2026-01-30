 #     <!--  -->
#the above is a comment in html

'''
Create superuser: 
ruk 
baichoorukhsaar@gmail.com 
ruk58140367 


admin2
Admin2@123

staff1
Staff1@123

staff2
Staff2@123

staff3
Staff3@123
'''
#code below
'''
py manage.py shell

from django.contrib.auth.models import User
from retail.models import UserProfile

def create_user_with_role(username, password, role):
    user, created = User.objects.get_or_create(username=username)
    if created:
        user.set_password(password)

        if role == "owner":
            user.is_staff = True
            user.is_superuser = True
        elif role == "staff":
            user.is_staff = True
            user.is_superuser = False
        user.save()

    profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"role": role})
    if profile.role != role:
        profile.role = role
        profile.save()

    print(f"OK: {username} -> {role}")

# 1 more owner/admin
create_user_with_role("admin2", "Admin2@123", "owner")

# 3 staff
create_user_with_role("staff1", "Staff1@123", "staff")
create_user_with_role("staff2", "Staff2@123", "staff")
create_user_with_role("staff3", "Staff3@123", "staff")

'''

print ("Hello, World!")
