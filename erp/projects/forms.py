from django import forms
from django.contrib.auth import get_user_model
from django.forms import BaseInlineFormSet, inlineformset_factory

from core.models import Floor, ItemPrice, OrderItem, Partner, Project, ProjectType, Section, SectionFile

User = get_user_model()


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format="%Y-%m-%d")


def _dates(form):
    for f in form.fields.values():
        if isinstance(f, forms.DateField):
            f.widget = DateInput()


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = [
            "name", "project_type", "status", "contract_date", "start_date", "end_date",
            "customer", "customer_contact", "contractor", "contractor_contact", "manager", "note",
        ]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self)
        inst = self.instance
        self.fields["project_type"].queryset = ProjectType.objects.filter(is_active=True) | ProjectType.objects.filter(
            pk=inst.project_type_id or 0
        )
        self.fields["customer"].queryset = Partner.objects.filter(is_customer=True, is_active=True) | Partner.objects.filter(
            pk=inst.customer_id or 0
        )
        self.fields["contractor"].queryset = Partner.objects.filter(is_contractor=True, is_active=True) | Partner.objects.filter(
            pk=inst.contractor_id or 0
        )
        self.fields["customer"].widget.attrs["data-quick"] = "customer"
        self.fields["contractor"].widget.attrs["data-quick"] = "contractor"
        self.fields["manager"].queryset = User.objects.filter(is_active=True)
        self.fields["manager"].label_from_instance = lambda u: u.get_full_name() or u.get_username()

    def clean(self):
        data = super().clean()
        s, e = data.get("start_date"), data.get("end_date")
        if s and e and e < s:
            self.add_error("end_date", "종료일은 시작일 이후여야 합니다.")
        return data


class OrderItemForm(forms.ModelForm):
    class Meta:
        model = OrderItem
        fields = ["item_name", "unit", "quantity", "amount", "note"]

    def clean_quantity(self):
        v = self.cleaned_data.get("quantity")
        if v is not None and v < 0:
            raise forms.ValidationError("수량은 0 이상이어야 합니다.")
        return v

    def clean_amount(self):
        v = self.cleaned_data.get("amount")
        if v is not None and v < 0:
            raise forms.ValidationError("금액은 0 이상이어야 합니다.")
        return v


class ItemPriceForm(forms.ModelForm):
    class Meta:
        model = ItemPrice
        fields = ["spec", "unit", "unit_price", "effective_from", "note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self)

    def clean_unit_price(self):
        v = self.cleaned_data.get("unit_price")
        if v is not None and v < 0:
            raise forms.ValidationError("단가는 0 이상이어야 합니다.")
        return v


class SectionForm(forms.ModelForm):
    class Meta:
        model = Section
        fields = [
            "name", "floor", "planned_ship_date", "confirmed_ship_date", "is_completed",
            "make_type", "fabricator", "fabricator_reviewed_on", "note",
        ]
        widgets = {"note": forms.TextInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self)
        self.fields["floor"].widget.attrs["data-quick"] = "floor"
        inst = self.instance
        self.fields["fabricator"].queryset = Partner.objects.filter(is_fabricator=True, is_active=True) | Partner.objects.filter(
            pk=inst.fabricator_id or 0
        )
        self.fields["fabricator"].widget.attrs["data-quick"] = "fabricator"


class BaseSectionFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen = {}
        blocked = []
        for form in self.forms:
            cd = getattr(form, "cleaned_data", None)
            if not cd or "name" not in cd:
                continue
            if cd.get("DELETE"):
                if form.instance.pk and form.instance.bom_items.exists():
                    msg = "이 구간에는 BOM이 등록되어 있어 삭제할 수 없습니다. 먼저 구간 BOM 화면에서 BOM을 정리하세요."
                    form.add_error(None, msg)
                    blocked.append(cd["name"])
                continue
            floor = cd.get("floor")
            key = (cd["name"].strip(), floor.pk if floor else None)
            if key in seen:
                form.add_error("name", "같은 프로젝트에 이름과 층이 같은 구간이 이미 있습니다.")
            seen[key] = True
        if blocked:
            raise forms.ValidationError(
                "BOM이 등록되어 있어 삭제할 수 없는 구간이 있습니다: %(names)s. 삭제 표시를 해제하세요.",
                params={"names": ", ".join(blocked)},
            )


class BasePriceFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            cd = getattr(form, "cleaned_data", None)
            if not cd or "spec" not in cd or cd.get("DELETE"):
                continue
            key = (cd["spec"].strip(), cd.get("effective_from"))
            if key in seen:
                form.add_error("spec", "같은 규격에 같은 적용 시작일의 단가가 이미 있습니다.")
            seen.add(key)


OrderItemFormSet = inlineformset_factory(Project, OrderItem, form=OrderItemForm, extra=1, can_delete=True)
ItemPriceFormSet = inlineformset_factory(
    Project, ItemPrice, form=ItemPriceForm, formset=BasePriceFormSet, extra=1, can_delete=True
)
SectionFormSet = inlineformset_factory(
    Project, Section, form=SectionForm, formset=BaseSectionFormSet, extra=1, can_delete=True
)


class SectionFileForm(forms.ModelForm):
    class Meta:
        model = SectionFile
        fields = ["section", "distributed_on", "design_changed_on", "change_note", "file", "note"]

    def __init__(self, *args, project=None, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self)
        self.fields["section"].queryset = Section.objects.filter(project=project).select_related("floor")
        self.fields["section"].empty_label = "(구간 지정 안 함)"


class QuickPartnerForm(forms.Form):
    name = forms.CharField(max_length=100)
    role = forms.ChoiceField(choices=[("customer", ""), ("contractor", ""), ("fabricator", "")])


class QuickFloorForm(forms.Form):
    name = forms.CharField(max_length=30)
