# 건설자재 ERP (1차 버전)

프로젝트·구간 등록, 구간 BOM 업로드, 진행현황 조회를 다루는 내부 ERP 1차 버전입니다.

## 요구 사항

- Python 3.13

## 로컬 실행

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

브라우저에서 http://127.0.0.1:8000 에 접속한 뒤 데모 계정으로 로그인합니다.

| 계정 | 비밀번호 | 비고 |
| --- | --- | --- |
| admin | admin1234 | 관리자 (superuser) |
| demo1 | demo1234 | 일반 사용자 |
| demo2 | demo1234 | 일반 사용자 |

`seed_demo`는 여러 번 실행해도 데이터가 중복 생성되지 않습니다.

## 테스트

```bash
python manage.py test
```

## 샘플 BOM 파일

`samples/sample_bom.xlsx` 에 BOM 업로드 테스트용 샘플이 있습니다. 파일을 다시 만들려면 다음을 실행합니다.

```bash
python samples/make_sample_bom.py
```

## 주의

데모 계정의 비밀번호는 공개된 값이므로, 실제 사용 전에 반드시 변경하거나 계정을 삭제하세요.
