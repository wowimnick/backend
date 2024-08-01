from django.db import models
from storages.backends.s3boto3 import S3Boto3Storage

class BusinessInfo(models.Model):
    businessName = models.CharField(max_length=100)
    businessId = models.AutoField(primary_key=True)
    businessImage = models.ImageField(upload_to='business_images/', storage=S3Boto3Storage(), blank=True, null=True)
    businessType = models.CharField(max_length=100)
    businessAddress = models.CharField(max_length=100)
    businessDescription = models.CharField(max_length=100, blank=True, null=True)
    businessCity = models.CharField(max_length=100)
    businessState = models.CharField(max_length=100, blank=True, null=True)
    businessPhoneNumber = models.CharField(max_length=100)
    businessEmail = models.CharField(max_length=100, blank=True, null=True)
    createdAt = models.DateTimeField()
    businessDefaultCancellation = models.CharField(max_length=100, blank=True, null=True)
    userId = models.ForeignKey('Users', models.DO_NOTHING, db_column='userId', blank=True, null=True)
    businessZipCode = models.CharField(max_length=100)
    totalReviews = models.IntegerField(default=0)

    def update_total_reviews(self):
        self.totalReviews = Reviews.objects.filter(
            classId__businessId=self
        ).count()
        self.save()

    class Meta:
        db_table = 'businessInfo'

class ClassImage(models.Model):
    imageId = models.AutoField(primary_key=True)
    classId = models.ForeignKey('ClassesMain', related_name='images', on_delete=models.CASCADE)
    image = models.ImageField(upload_to='class_images/', storage=S3Boto3Storage())
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'classImages'

class ClassesMain(models.Model):
    classId = models.AutoField(primary_key=True)
    className = models.CharField(max_length=50)
    classDescription = models.CharField(max_length=2000, blank=True, null=True)
    classLocation = models.CharField(max_length=100)
    classCoordinates = models.CharField(max_length=100)
    classRating = models.CharField(max_length=100)
    classFeatures = models.CharField(max_length=1000)
    classPrice = models.IntegerField()
    classCategory = models.CharField(max_length=100)
    classFilterCategory = models.CharField(max_length=100)
    classFilterSubcategory = models.CharField(max_length=100)
    businessId = models.ForeignKey(BusinessInfo, models.DO_NOTHING, db_column='businessId', blank=True, null=True)
    classTotalReviews = models.IntegerField()
    additionalInfo = models.CharField(max_length=5000, blank=True, null=True)
    createdAt = models.DateTimeField()

    class Meta:
        db_table = 'classesMain'


class Enrollments(models.Model):
    enrollmentId = models.AutoField(primary_key=True)
    userId = models.ForeignKey('Users', models.DO_NOTHING, db_column='userId', blank=True, null=True)
    scheduleId = models.ForeignKey('Schedules', models.DO_NOTHING, db_column='scheduleId', blank=True, null=True)
    status = models.CharField(max_length=100)
    createdAt = models.DateTimeField()

    class Meta:
        db_table = 'enrollments'


class Favorites(models.Model):
    favoriteId = models.AutoField(primary_key=True)
    userId = models.ForeignKey('Users', models.DO_NOTHING, db_column='userId', blank=True, null=True)
    classId = models.ForeignKey(ClassesMain, models.DO_NOTHING, db_column='classId', blank=True, null=True)
    createdAt = models.DateTimeField()

    class Meta:
        db_table = 'favorites'


class Reviews(models.Model):
    reviewId = models.AutoField(primary_key=True)
    userId = models.ForeignKey('Users', models.DO_NOTHING, db_column='userId', blank=True, null=True)
    businessId = models.ForeignKey(BusinessInfo, models.DO_NOTHING, db_column='businessId', blank=True, null=True)
    classId = models.ForeignKey(ClassesMain, models.DO_NOTHING, db_column='classId', blank=True, null=True)
    rating = models.IntegerField()
    comment = models.TextField(blank=True, null=True)
    createdAt = models.DateTimeField()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.classId and self.classId.businessId:
            self.classId.businessId.update_total_reviews()

    class Meta:
        db_table = 'reviews'


class Schedules(models.Model):
    scheduleId = models.AutoField(primary_key=True)
    subclassId = models.ForeignKey('SubClasses', models.DO_NOTHING, db_column='subclassId', blank=True, null=True)
    startTime = models.DateTimeField()
    endTime = models.DateTimeField()
    availableSlots = models.IntegerField()
    createdAt = models.DateTimeField()

    class Meta:
        db_table = 'schedules'


class SubClasses(models.Model):
    subclassId = models.AutoField(primary_key=True)
    classId = models.ForeignKey(ClassesMain, models.DO_NOTHING, db_column='classId', blank=True, null=True)
    subclassTitle = models.CharField(max_length=100)
    subclassDescription = models.CharField(max_length=100, blank=True, null=True)
    subclassImage = models.ImageField(upload_to='subclass_images/', storage=S3Boto3Storage(), blank=True, null=True)
    subclassTags = models.CharField(max_length=100)
    subclassType = models.CharField(max_length=100)
    subclassLevel = models.CharField(max_length=100)
    subclassAvailability = models.IntegerField()
    subclassPrice = models.IntegerField()
    subclassCategory = models.CharField(max_length=100)
    subclassCalenderDuration = models.CharField(max_length=100)
    createdAt = models.DateTimeField()

    class Meta:
        db_table = 'subClasses'


class Users(models.Model):
    userId = models.AutoField(primary_key=True)
    firstName = models.CharField(max_length=100)
    lastName = models.CharField(max_length=100)
    email = models.CharField(unique=True, max_length=100)
    password = models.CharField(max_length=100)
    phoneNumber = models.CharField(max_length=100, blank=True, null=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    zipCode = models.CharField(max_length=100)
    createdAt = models.DateTimeField()

    class Meta:
        db_table = 'users'
