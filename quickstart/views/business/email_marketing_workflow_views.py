"""Marketing automation workflows (Business+)."""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from quickstart.models import Contact, MarketingWorkflow, MarketingWorkflowEnrollment, MarketingWorkflowStep
from quickstart.services.email_marketing_config import price_id_to_tier
from quickstart.views.business.email_marketing_views import _require_marketing
from quickstart.utils.permissions import CanManageEmailMarketing, CanManageOwnClasses


def _tier_workflow_error(tier, activating: bool, total_active_after: int, step_count: int):
    if not tier.get("automation_enabled"):
        return "Automations require Business or Scale email marketing."
    max_wf = tier.get("max_active_workflows", 0)
    if activating and total_active_after > max_wf:
        return f"Active workflow limit reached ({max_wf})."
    max_steps = tier.get("max_workflow_steps", 0)
    if step_count > max_steps:
        return f"Workflow step limit is {max_steps} on your plan."
    return None


class MarketingWorkflowListCreateView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business, _, err = _require_marketing(request)
        if err:
            return err
        qs = MarketingWorkflow.objects.filter(business=business)
        return Response(
            [
                {
                    "id": str(w.id),
                    "name": w.name,
                    "status": w.status,
                    "trigger_type": w.trigger_type,
                    "trigger_config": w.trigger_config,
                    "updated_at": w.updated_at.isoformat(),
                }
                for w in qs
            ]
        )

    def post(self, request):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        if not tier.get("automation_enabled"):
            return Response({"error": "Automations require Business or Scale."}, status=403)
        name = (request.data.get("name") or "").strip() or "Untitled automation"
        w = MarketingWorkflow.objects.create(
            business=business,
            name=name[:255],
            status="draft",
            trigger_type=(request.data.get("trigger_type") or "manual").strip()[:64],
            trigger_config=request.data.get("trigger_config")
            if isinstance(request.data.get("trigger_config"), dict)
            else {},
        )
        return Response({"id": str(w.id)}, status=201)


class MarketingWorkflowDetailView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request, workflow_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        w = get_object_or_404(MarketingWorkflow, id=workflow_id, business=business)
        steps = w.steps.all().order_by("order")
        return Response(
            {
                "id": str(w.id),
                "name": w.name,
                "status": w.status,
                "trigger_type": w.trigger_type,
                "trigger_config": w.trigger_config,
                "steps": [
                    {
                        "id": str(s.id),
                        "order": s.order,
                        "step_type": s.step_type,
                        "config": s.config,
                    }
                    for s in steps
                ],
            }
        )

    def put(self, request, workflow_id):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        w = get_object_or_404(MarketingWorkflow, id=workflow_id, business=business)
        if w.status not in ("draft", "active", "paused"):
            return Response({"error": "Workflow is locked."}, status=400)

        if request.data.get("name") is not None:
            w.name = str(request.data.get("name"))[:255]
        if request.data.get("trigger_type") is not None:
            w.trigger_type = str(request.data.get("trigger_type"))[:64]
        if request.data.get("trigger_config") is not None and isinstance(
            request.data.get("trigger_config"), dict
        ):
            w.trigger_config = request.data.get("trigger_config")

        steps_payload = request.data.get("steps")
        if isinstance(steps_payload, list):
            e = _tier_workflow_error(tier, False, 0, len(steps_payload))
            if e:
                return Response({"error": e}, status=403)
            w.steps.all().delete()
            for i, row in enumerate(steps_payload):
                if not isinstance(row, dict):
                    continue
                MarketingWorkflowStep.objects.create(
                    workflow=w,
                    order=int(row.get("order", i)),
                    step_type=str(row.get("step_type") or "delay")[:32],
                    config=row.get("config") if isinstance(row.get("config"), dict) else {},
                )

        new_status = request.data.get("status")
        if new_status in ("draft", "active", "paused"):
            if new_status == "active":
                others_active = MarketingWorkflow.objects.filter(
                    business=business, status="active"
                ).exclude(pk=w.pk).count()
                activating = w.status != "active"
                total_if_active = others_active + 1
                e = _tier_workflow_error(
                    tier, activating, total_if_active, w.steps.count()
                )
                if e and activating:
                    return Response({"error": e}, status=403)
                if w.steps.count() == 0:
                    return Response(
                        {"error": "Add at least one step before activating."},
                        status=400,
                    )
            w.status = new_status
        w.save()
        return Response({"id": str(w.id)})

    def delete(self, request, workflow_id):
        business, _, err = _require_marketing(request)
        if err:
            return err
        w = get_object_or_404(MarketingWorkflow, id=workflow_id, business=business)
        w.delete()
        return Response(status=204)


class MarketingWorkflowEnrollView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def post(self, request, workflow_id):
        business, addon, err = _require_marketing(request)
        if err:
            return err
        tier = price_id_to_tier(addon.stripe_price_id)
        if not tier.get("automation_enabled"):
            return Response({"error": "Automations require Business or Scale."}, status=403)
        w = get_object_or_404(MarketingWorkflow, id=workflow_id, business=business)
        if w.status != "active":
            return Response({"error": "Workflow must be active to enroll."}, status=400)
        ids = request.data.get("contact_ids")
        if not isinstance(ids, list) or not ids:
            return Response({"error": "contact_ids required."}, status=400)
        from django.utils import timezone

        enrolled = 0
        for cid in ids:
            contact = Contact.objects.filter(id=cid, business=business).first()
            if not contact or not (contact.email or "").strip():
                continue
            _, created = MarketingWorkflowEnrollment.objects.get_or_create(
                workflow=w,
                contact=contact,
                defaults={
                    "current_step_index": 0,
                    "next_run_at": timezone.now(),
                    "status": "active",
                },
            )
            if created:
                enrolled += 1
        return Response({"enrolled": enrolled})


class MarketingWorkflowEnrollmentListView(APIView):
    permission_classes = [IsAuthenticated, CanManageEmailMarketing]

    def get(self, request):
        business, _, err = _require_marketing(request)
        if err:
            return err
        wf_id = request.query_params.get("workflow_id")
        qs = MarketingWorkflowEnrollment.objects.filter(
            workflow__business=business
        ).select_related("contact", "workflow")
        if wf_id:
            qs = qs.filter(workflow_id=wf_id)
        qs = qs.order_by("-created_at")[:200]
        return Response(
            [
                {
                    "id": str(e.id),
                    "workflow_id": str(e.workflow_id),
                    "contact_id": str(e.contact_id),
                    "email": e.contact.email,
                    "status": e.status,
                    "current_step_index": e.current_step_index,
                    "next_run_at": e.next_run_at.isoformat() if e.next_run_at else None,
                }
                for e in qs
            ]
        )
