import factory
from factory.django import DjangoModelFactory
from django.utils import timezone
from datetime import date, time, timedelta
from decimal import Decimal
from django.utils.text import slugify

from quickstart.models import (
    CustomUser,
    BusinessInfo,
    ClassCategory,
    ClassSubcategory,
    ClassesMain,
    ClassOption,
    Payment,
    Schedule,
    ScheduleInstance,
    Booking,
    Reviews,
    Role,
    SupportTicket,
    VerificationRequest,
    Payout,
    BlogCategory,
    BlogPost,
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


class BlogCategoryFactory(DjangoModelFactory):
    class Meta:
        model = BlogCategory
        django_get_or_create = ("name",)

    name = factory.Faker("word")
    slug = factory.LazyAttribute(lambda o: slugify(o.name))


class BlogPostFactory(DjangoModelFactory):
    class Meta:
        model = BlogPost
        django_get_or_create = ("slug",)

    title = factory.Faker("sentence", nb_words=5)
    slug = factory.LazyAttribute(lambda o: slugify(o.title))
    excerpt = factory.Faker("paragraph", nb_sentences=2)
    content = factory.Faker("text", max_nb_chars=1000)
    image_url = factory.Faker("image_url")
    author = factory.SubFactory(UserFactory)
    category = factory.SubFactory(BlogCategoryFactory)
    tags = factory.LazyFunction(lambda: ["testing", "django", "api"])
    status = factory.Iterator(["published", "draft"])
    published_date = factory.LazyFunction(timezone.now)


class ClassCategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassCategory
        django_get_or_create = ("key",)

    name = factory.Faker("word")
    key = factory.LazyAttribute(lambda o: o.name.lower())


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
    category = factory.SubFactory(ClassCategoryFactory)
    subcategory = factory.SubFactory(
        ClassSubcategoryFactory, category=factory.SelfAttribute("..category")
    )
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
    date = factory.Sequence(lambda n: (timezone.now() + timedelta(days=n)).date())
    time = time(14, 0)
    duration = 60
    price = Decimal("25.00")
    maxParticipants = 10


class ScheduleInstanceFactory(DjangoModelFactory):
    class Meta:
        model = ScheduleInstance

    schedule = factory.SubFactory(ScheduleFactory)
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


class PayoutFactory(DjangoModelFactory):
    class Meta:
        model = Payout

    business = factory.SubFactory(BusinessInfoFactory)
    stripe_transfer_id = factory.Sequence(lambda n: f"tr_test_{n}")
    amount = factory.Faker(
        "pydecimal", left_digits=4, right_digits=2, positive=True, min_value=50
    )
    currency = "cad"
    arrival_date = factory.LazyFunction(
        lambda: timezone.now().date() + timedelta(days=7)
    )
    status = factory.Iterator(["pending", "in_transit", "paid", "failed"])
    created_at = factory.LazyFunction(timezone.now)

    @factory.post_generation
    def bookings(self, create, extracted, **kwargs):
        if not create:
            return
        if extracted:
            for booking in extracted:
                self.bookings.add(booking)


class ReviewFactory(DjangoModelFactory):
    class Meta:
        model = Reviews

    userId = factory.SubFactory(UserFactory)
    classId = factory.SubFactory(ClassesMainFactory)
    businessId = factory.SelfAttribute("classId.businessId")
    booking = factory.LazyAttribute(
        lambda o: BookingFactory(
            user=o.userId,
            schedule_instance__schedule__option__classId=o.classId,
        )
    )
    rating = factory.Faker("pyint", min_value=4, max_value=5)
    comment = factory.Faker("text")
    status = "approved"


class SupportTicketFactory(DjangoModelFactory):
    class Meta:
        model = SupportTicket

    user = factory.SubFactory(UserFactory)
    subject = factory.Faker("sentence", nb_words=6)
    description = factory.Faker("text", max_nb_chars=300)
    category = "technical"
    status = "open"
    priority = "medium"


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
