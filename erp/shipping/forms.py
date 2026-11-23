from django import forms

from core.models import Carrier, Partner, Plant, Shipment, ShipmentTruck, VehicleType


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format="%Y-%m-%d")


def _dates(form):
    for f in form.fields.values():
        if isinstance(f, forms.DateField):
            f.widget = DateInput()


def _active_or_current(model, current_pk):
    """Active rows plus the one already chosen (so an old choice stays visible)."""
    return model.objects.filter(is_active=True) | model.objects.filter(pk=current_pk or 0)


# ---------------------------------------------------------------- master data


class PlantForm(forms.ModelForm):
    class Meta:
        model = Plant
        fields = ["name", "site", "operator", "sort_order", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["operator"].queryset = _active_or_current(Partner, self.instance.operator_id)
        self.fields["operator"].empty_label = "자사 운영 (선택 안 함)"
        self.fields["operator"].help_text = "외주 업체가 운영하는 제작장이면 업체를 고르세요. 비워 두면 자사 운영입니다."
        self.fields["site"].help_text = "같은 공장 안의 제작장은 같은 위치로 적으면 목록에서 묶여 보입니다."
        self.fields["is_active"].help_text = "체크를 풀면 새 출하의 선택 목록에서 빠집니다(기존 출하는 그대로)."


class CarrierForm(forms.ModelForm):
    class Meta:
        model = Carrier
        fields = ["name", "phone", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].help_text = "체크를 풀면 새 선택 목록에서 빠집니다."


class VehicleTypeForm(forms.ModelForm):
    class Meta:
        model = VehicleType
        fields = ["name", "sort_order", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].help_text = "체크를 풀면 새 선택 목록에서 빠집니다."


# ---------------------------------------------------------------- shipment


class ShipmentForm(forms.ModelForm):
    class Meta:
        model = Shipment
        fields = ["project", "ship_date", "arrival_date", "plant", "dispatcher", "note"]
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _dates(self)
        inst = self.instance
        self.fields["project"].empty_label = "선택하세요"
        if inst.pk:
            self.fields["project"].disabled = True
            self.fields["project"].help_text = "프로젝트는 바꿀 수 없습니다."
        self.fields["plant"].queryset = _active_or_current(Plant, inst.plant_id)
        self.fields["plant"].empty_label = "선택 안 함"

    def clean(self):
        data = super().clean()
        s, a = data.get("ship_date"), data.get("arrival_date")
        if s and a and a < s:
            self.add_error("arrival_date", "도착일은 출하일 이후여야 합니다.")
        return data


class TruckForm(forms.ModelForm):
    class Meta:
        model = ShipmentTruck
        fields = ["carrier", "vehicle_type", "plate_no", "driver_name", "driver_phone", "load_note"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        inst = self.instance
        self.fields["carrier"].queryset = _active_or_current(Carrier, inst.carrier_id)
        self.fields["vehicle_type"].queryset = _active_or_current(VehicleType, inst.vehicle_type_id)
        self.fields["carrier"].empty_label = "선택 안 함"
        self.fields["vehicle_type"].empty_label = "선택 안 함"


class CancelForm(forms.Form):
    reason = forms.CharField(
        label="취소 사유",
        max_length=200,
        error_messages={"required": "취소 사유를 입력하세요.", "max_length": "취소 사유는 200자 이하로 입력하세요."},
    )

    def clean_reason(self):
        v = self.cleaned_data["reason"].strip()
        if not v:
            raise forms.ValidationError("취소 사유를 입력하세요.")
        return v
