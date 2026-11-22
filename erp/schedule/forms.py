from django import forms
from django.contrib.auth import get_user_model

from core.models import Project

from . import selectors

DATE_ERR = "올바른 날짜를 입력하세요. 예: 2026-03-18"


class BoardFilterForm(forms.Form):
    start = forms.DateField(label="점검 시작일", required=False, widget=forms.DateInput(attrs={"type": "date"}),
                            error_messages={"invalid": DATE_ERR})
    end = forms.DateField(label="점검 종료일", required=False, widget=forms.DateInput(attrs={"type": "date"}),
                          error_messages={"invalid": DATE_ERR})
    project = forms.ModelChoiceField(label="프로젝트", required=False, queryset=Project.objects.none(),
                                     empty_label="전체", error_messages={"invalid_choice": "선택할 수 없는 프로젝트입니다."})
    status = forms.ChoiceField(label="프로젝트 상태", required=False,
                               choices=[("", "진행 중 (완료 제외)")] + list(Project.Status.choices),
                               error_messages={"invalid_choice": "선택할 수 없는 상태입니다."})
    manager = forms.ModelChoiceField(label="담당자", required=False, queryset=get_user_model().objects.none(),
                                     empty_label="전체", error_messages={"invalid_choice": "선택할 수 없는 담당자입니다."})
    late_only = forms.BooleanField(label="지연만", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].queryset = Project.objects.order_by("-code")
        self.fields["manager"].queryset = selectors.managers()
        self.fields["manager"].label_from_instance = lambda u: u.get_full_name() or u.get_username()

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["start"] > data["end"]:
            self.add_error("end", "종료일은 시작일 이후여야 합니다.")
        return data


class ActualDateForm(forms.Form):
    actual_date = forms.DateField(
        label="실적일",
        error_messages={"required": "실적일을 입력하세요.", "invalid": DATE_ERR},
    )
