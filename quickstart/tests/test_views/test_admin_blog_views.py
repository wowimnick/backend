from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission

from quickstart.models import BlogPost, BlogCategory, Role
from quickstart.tests.factories import (
    UserFactory,
    RoleFactory,
    BlogPostFactory,
    BlogCategoryFactory,
)


def _get_and_assign_permissions(role, permissions_map):
    for model_class, codenames in permissions_map.items():
        content_type = ContentType.objects.get_for_model(model_class)
        for codename in codenames:
            permission, _ = Permission.objects.get_or_create(
                codename=codename,
                content_type=content_type,
            )
            role.permissions.add(permission)


class AdminBlogManagementTests(APITestCase):
    def setUp(self):
        self.admin_role = RoleFactory(name="Admin", hierarchy_level=80)
        _get_and_assign_permissions(
            self.admin_role,
            {
                BlogPost: ["access_blog_admin"],
                BlogCategory: ["manage_blog_categories"],
            },
        )
        self.admin_user = UserFactory(role=self.admin_role)
        self.admin_user.user_permissions.add(*self.admin_role.permissions.all())
        self.non_admin_user = UserFactory()

        self.category1 = BlogCategoryFactory(name="Tutorials")
        self.category2 = BlogCategoryFactory(name="News")
        self.post1 = BlogPostFactory(
            category=self.category1, author=self.admin_user, title="First Post"
        )
        self.post2 = BlogPostFactory(category=self.category1, status="draft")

        self.client.force_authenticate(user=self.admin_user)

    def test_admin_can_list_blog_posts(self):
        """
        GET /api/platform-admin/blog/posts/ - Admin can list blog posts.
        """
        url = reverse("admin-blog-posts-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["results"]), 2)

    def test_non_admin_cannot_access_blog_posts(self):
        """
        GET /api/platform-admin/blog/posts/ - Non-admin is denied access.
        """
        self.client.force_authenticate(user=self.non_admin_user)
        url = reverse("admin-blog-posts-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_create_blog_post(self):
        """
        POST /api/platform-admin/blog/posts/ - Admin can create a new blog post.
        """
        url = reverse("admin-blog-posts-list")
        data = {
            "title": "A Brand New Post",
            "slug": "a-brand-new-post",
            "excerpt": "This is a test excerpt.",
            "content": "Full content goes here.",
            "image_url": "http://example.com/image.png",
            "category": self.category2.pk,
            "status": "published",
        }
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_post = BlogPost.objects.get(slug="a-brand-new-post")
        self.assertEqual(new_post.author, self.admin_user)
        self.assertEqual(new_post.category, self.category2)

    def test_admin_can_update_blog_post(self):
        """
        PATCH /api/platform-admin/blog/posts/{pk}/ - Admin can update a blog post.
        """
        url = reverse("admin-blog-posts-detail", kwargs={"pk": self.post1.pk})
        data = {"title": "Updated Title"}
        response = self.client.patch(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.post1.refresh_from_db()
        self.assertEqual(self.post1.title, "Updated Title")

    def test_admin_can_delete_blog_post(self):
        """
        DELETE /api/platform-admin/blog/posts/{pk}/ - Admin can delete a blog post.
        """
        url = reverse("admin-blog-posts-detail", kwargs={"pk": self.post2.pk})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(BlogPost.objects.filter(pk=self.post2.pk).exists())

    def test_admin_can_list_blog_categories(self):
        """
        GET /api/platform-admin/blog/categories/ - Admin can list blog categories.
        """
        url = reverse("admin-blog-categories-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        category_data = next(
            item for item in response.data if item["name"] == "Tutorials"
        )
        self.assertEqual(category_data["post_count"], 2)

    def test_admin_can_create_blog_category(self):
        """
        POST /api/platform-admin/blog/categories/ - Admin can create a category.
        """
        url = reverse("admin-blog-categories-list")
        data = {"name": "Interviews", "slug": "interviews"}
        response = self.client.post(url, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(BlogCategory.objects.filter(slug="interviews").exists())

    def test_cannot_delete_category_with_posts(self):
        """
        DELETE .../blog/categories/{pk}/ - Cannot delete a category with posts assigned.
        """
        url = reverse("admin-blog-categories-detail", kwargs={"pk": self.category1.pk})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(BlogCategory.objects.filter(pk=self.category1.pk).exists())

    def test_admin_can_delete_empty_blog_category(self):
        """
        DELETE .../blog/categories/{pk}/ - Can delete a category with no posts.
        """
        empty_category = BlogCategoryFactory(name="Empty")
        url = reverse("admin-blog-categories-detail", kwargs={"pk": empty_category.pk})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(BlogCategory.objects.filter(pk=empty_category.pk).exists())
