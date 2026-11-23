from django import forms

from core.models import BomItem


class ItemSpecForm(forms.ModelForm):
    """Widths (mm) and member kind of one BOM row.

    When the Profile can be read, the widths come from it (the model signal overwrites them),
    so the width inputs are read-only and ignored on save.
    """

    class Meta:
        model = BomItem
        fields = ["bottom_width", "side1_width", "side2_width", "member_kind"]

    def __init__(self, *args, widths_locked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.widths_locked = widths_locked
        self.fields["member_kind"].required = False
        self.fields["member_kind"].choices = [("", "선택 안 함")] + list(BomItem.MemberKind.choices)
        for name in ("bottom_width", "side1_width", "side2_width"):
            f = self.fields[name]
            f.required = False
            f.widget.attrs.update({"min": 0, "max": 100000, "inputmode": "numeric"})
            if widths_locked:
                f.disabled = True  # keeps the stored value and ignores posted data

    def clean(self):
        data = super().clean()
        if not self.widths_locked and data.get("bottom_width") is None and any(
            data.get(n) for n in ("side1_width", "side2_width")
        ):
            self.add_error("bottom_width", "측판 폭을 입력하려면 하판 폭도 입력하세요.")
        return data
