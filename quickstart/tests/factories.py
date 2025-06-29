import factory
from factory.django import DjangoModelFactory
from django.utils import timezone
from datetime import date, time, timedelta
from decimal import Decimal

from quickstart.models import (
    CustomUser,
    BusinessInfo,
    ClassCategory,
    ClassSubcategory,  # Added ClassSubcategory
    ClassesMain,
    ClassOption,
    Payment,
    Schedule,
    ScheduleInstance,
    Booking,
    Reviews,
    Role,
    VerificationRequest,
)


# --- Core Factories ---
class RoleFactory(DjangoModelFactory):
    class Meta:
        model = Role

    name = factory.Faker("job")
    hierarchy_level = factory.Sequence(lambda n: n)


class UserFactory(DjangoModelFactory):
    class Meta:
        model = CustomUser
        django_get_or_create = ("username",)

    username = factory.Faker("user_name")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    is_staff = False
    is_active = True


class ClassCategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassCategory
        django_get_or_create = ("key",)

    name = factory.Faker("word")
    key = factory.LazyAttribute(lambda o: o.name.lower())


# Added ClassSubcategoryFactory
class ClassSubcategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassSubcategory

    category = factory.SubFactory(ClassCategoryFactory)
    name = factory.Faker("word")
    key = factory.LazyAttribute(lambda o: o.name.lower())


class BusinessInfoFactory(DjangoModelFactory):
    class Meta:
        model = BusinessInfo

    owner = factory.SubFactory(UserFactory)
    businessName = factory.Faker("company")
    businessType = "school"
    businessDescription = factory.Faker("text", max_nb_chars=300)
    studentContactPhone = factory.Faker("phone_number")
    studentContactEmail = factory.Faker("email")
    preferredContact = "email"
    businessAddress = factory.Faker("street_address")
    businessCity = factory.Faker("city")
    businessState = factory.Faker("state_abbr")
    businessZipCode = factory.Faker("zipcode")
    openingTime = time(9, 0)
    closingTime = time(17, 0)
    classCategory = factory.SubFactory(ClassCategoryFactory)
    termsAccepted = True
    privacyAccepted = True
    verificationStatus = "verified"
    isActive = True
    business_timezone = "UTC"
    stripe_account_id = factory.Sequence(lambda n: f"acct_test_{n}")
    stripe_account_status = "active"


# --- Class and Scheduling Factories ---
class ClassesMainFactory(DjangoModelFactory):
    class Meta:
        model = ClassesMain

    businessId = factory.SubFactory(BusinessInfoFactory)
    title = factory.Faker("catch_phrase")
    description = factory.Faker("text", max_nb_chars=500)
    category = factory.LazyAttribute(lambda o: o.businessId.classCategory)
    subcategory = factory.SubFactory(
        ClassSubcategoryFactory, category=factory.SelfAttribute("..category")
    )  # Added subcategory
    status = "active"
    location = "Test Location"
    coordinates = "45.0000,-75.0000"


class ClassOptionFactory(DjangoModelFactory):
    class Meta:
        model = ClassOption

    classId = factory.SubFactory(ClassesMainFactory)
    booking_type = "Single Session"
    cancellationPolicy = "24h"
    cancellationRefundPercentage = 100


class ScheduleFactory(DjangoModelFactory):
    class Meta:
        model = Schedule

    option = factory.SubFactory(ClassOptionFactory)
    # Make the date sequential for single-session schedules
    # to guarantee uniqueness when a new Schedule is created.
    date = factory.Sequence(lambda n: (timezone.now() + timedelta(days=n)).date())
    time = time(14, 0)
    duration = 60
    price = Decimal("25.00")
    maxParticipants = 10


class ScheduleInstanceFactory(DjangoModelFactory):
    class Meta:
        model = ScheduleInstance

    schedule = factory.SubFactory(ScheduleFactory)
    # Inherit the (now unique) date from the parent schedule.
    # This ensures the (schedule, date) combination is always unique and consistent.
    date = factory.LazyAttribute(lambda o: o.schedule.date)
    time = factory.LazyAttribute(lambda o: o.schedule.time)
    duration = factory.LazyAttribute(lambda o: o.schedule.duration)
    price = factory.LazyAttribute(lambda o: o.schedule.price)
    max_participants = factory.LazyAttribute(lambda o: o.schedule.maxParticipants)
    status = "scheduled"


# --- Interaction Factories ---
class BookingFactory(DjangoModelFactory):
    class Meta:
        model = Booking

    schedule_instance = factory.SubFactory(ScheduleInstanceFactory)
    user = factory.SubFactory(UserFactory)
    status = "confirmed"
    participants = 1
    amount_paid = factory.LazyAttribute(
        lambda o: o.schedule_instance.price * o.participants
    )
    payment_status = "paid"


class ReviewFactory(DjangoModelFactory):
    class Meta:
        model = Reviews

    userId = factory.SubFactory(UserFactory)
    classId = factory.SubFactory(ClassesMainFactory)
    businessId = factory.SelfAttribute("classId.businessId")
    # Use LazyAttribute to correctly resolve the context of the review (o)
    # being created, ensuring the booking is for the correct user and class.
    booking = factory.LazyAttribute(
        lambda o: BookingFactory(
            user=o.userId,
            schedule_instance__schedule__option__classId=o.classId,
        )
    )
    rating = factory.Faker("pyint", min_value=4, max_value=5)
    comment = factory.Faker("text")
    status = "approved"


class PaymentFactory(DjangoModelFactory):
    class Meta:
        model = Payment

    booking = factory.SubFactory(BookingFactory)
    stripe_payment_intent_id = factory.Sequence(lambda n: f"pi_test_{n}")
    amount = factory.LazyAttribute(lambda o: o.booking.amount_paid)
    service_fee_amount = factory.LazyAttribute(lambda o: o.amount * Decimal("0.13"))
    status = "succeeded"
    payment_method_type = "card"
    card_brand = "visa"
    card_last4 = "4242"


class VerificationRequestFactory(DjangoModelFactory):
    class Meta:
        model = VerificationRequest

    user = factory.SubFactory(UserFactory)
    business = factory.SubFactory(
        BusinessInfoFactory, owner=factory.SelfAttribute("..user")
    )
    status = "pending"
