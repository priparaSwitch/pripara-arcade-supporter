# 프리파라HK 도우미 2.3 빌드

Windows x64에서 64비트 Python과 명시적으로 지정한 로컬 LLVM-MinGW 도구로 빌드합니다. 완성된 앱을 사용하는 사람에게 Python을 설치하도록 요구하지 않습니다. 이 절차는 기본 QR 리더와 코디 리모컨을 합친 앱을 만듭니다.

## 개발 환경

프로젝트 루트에서 가상환경과 고정 버전 의존성을 준비합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
```

LLVM-MinGW는 `scripts/toolchain.json`에 적힌 배포본과 무결성 정보를 참고해 별도로 준비합니다. `build_sender.py`는 도구를 내려받거나 개인 경로를 자동 탐색하지 않습니다. 아래 `$toolchain` 값은 자신이 준비한 도구의 최상위 폴더로 지정합니다.

```powershell
$toolchain = Read-Host "LLVM-MinGW 폴더"
.\.venv\Scripts\python.exe scripts/build_sender.py --toolchain $toolchain
```

`native/sender/PriParaFrameSender.cpp`로 `build/native/PriParaFrameSender64.dll`과 앱의 `app/native/PriParaFrameSender64.dll`을 만듭니다. x64 PE 형식과 공개 함수·ABI 버전을 확인하고, 별도의 시험용 sender와 `selftest_receiver.cpp`를 빌드해 원본 공유메모리 수신 방식과 대조합니다. 시험 채널은 50/51로 제한하며, 실제 카메라 채널 0/1과 게임에는 연결하지 않습니다. 두 채널의 영상·방향·헤더, 잘못된 입력, 수신기 재개, 640×480 프레임 확인이 모두 통과한 뒤 앱 DLL을 교체합니다. 결과와 SHA-256은 `build/native/sender-build-report.json`에 남깁니다.

컴파일할 때 소스·도구 경로에 prefix map을 적용합니다. 사전 빌드된 정적 C++ 런타임에 남은 상위 도구의 CI 소스 경로는 최종 DLL의 읽기 전용 `.rdata`에 있는 NUL 종료 `.cpp`/`.h` 문자열에 한해 파일명으로 정규화합니다. 원래 길이는 0으로 채워 주소와 이미지 크기를 유지하며, 코드와 재배치 섹션이 바이트 단위로 동일한지도 확인합니다. 후처리한 시험 DLL로도 같은 수신 검증을 수행합니다. 원문 경로는 증명 파일에 기록하지 않습니다.

가상카메라 필터를 새로 만들거나 설치·등록하지 않습니다. 기존 `PriPara QR Camera` 드라이버는 그대로 사용합니다. 상부 카메라는 이 앱의 빌드·사용 대상에 포함하지 않습니다.

## 외부 데이터 준비

코디 데이터는 앱 EXE와 분리한 `data.db`를 사용합니다. `images.bin`은 선택 사항이며, 없으면 그림 없이 목록과 검색을 사용할 수 있습니다. 원본 카탈로그와 네이티브 아이템 표, 공개용 파츠명·문자표 JSON을 준비한 다음 각 경로를 명시합니다.

이미 준비된 Releases 실행 패키지의 `data.db`와 `images.bin`을 프로젝트 루트에 복사해 빌드할 수도 있습니다. 두 파일은 소스 저장소와 분리되어 있으며 개인 티켓 색인 DB나 복호화 자료를 대신 넣으면 안 됩니다.

```powershell
$catalog = Read-Host "원본 카탈로그 DB"
$itemTable = Read-Host "원본 아이템 표"
$parts = Read-Host "파츠명 JSON"
$nameTable = Read-Host "이름 문자표 JSON"
.\.venv\Scripts\python.exe tools/prepare_suite_data.py --catalog $catalog --item-table $itemTable --parts $parts --name-table $nameTable --output .
```

그림을 포함하려면 `--images`로 원본 이미지 묶음을 추가합니다. 경량 출력을 원하면 별도의 출력 폴더를 사용하거나 기존 `images.bin`을 직접 정리해야 합니다. 도구는 남아 있는 이미지 파일을 임의로 삭제하지 않습니다. 입력 파일을 출력 파일과 같은 위치로 지정할 수 없습니다.

준비 도구는 카탈로그의 항목·세트·기존 메타 정보를 보존하고, 각 아이템에 네이티브 QR용 보조값만 추가합니다. 누락·충돌·지원하지 않는 형식은 실패하며, 결과 파일과 SHA·개수 증명은 검증이 끝난 뒤 교체합니다. 사용자 티켓·프로필·설정 DB는 빌드 입력에 포함하지 않습니다.

캐릭터 생성용 공개 카탈로그와 참조 이미지는 `tools/prepare_character_assets.py --source <공개 웹앱 public 폴더> --output app/character_assets`로 준비합니다. 허용된 메타 필드와 상대 이미지 경로만 복사하며 공개 고정 시작 QR의 분류와 SHA를 기록합니다.

얼굴 모델 묶음은 기존 아틀리에의 준비 도구와 명시적 로컬 원본으로 생성합니다. `tools/prepare_portrait_assets.py --help`에 입력 항목이 있습니다. UnityPy는 이 준비 단계에서만 필요합니다. 비공개 원본 manifest·Unity 번들·중간 캐시는 배포하지 않고, 공개 DTO로 정리한 geometry·텍스처·원본 렌더러·허가문만 `images.bin`의 `_portrait/`에 넣습니다. 기존 의상 이미지 바이트와 geometry/UV는 유지하며, 텍스처를 줄일 때 투명 픽셀의 RGB도 보존합니다. 이 준비 도구의 UnityPy 의존성과 런타임 앱 의존성은 구분합니다.

## 앱 패키지

```powershell
.\.venv\Scripts\python.exe scripts/build_app.py
```

출력 폴더는 `build/app-dist/PriParaImageCamera/`입니다. EXE와 `_internal/`, 외부 `data.db`를 함께 사용하며, 그림을 제공할 때만 `images.bin`을 추가합니다. `_internal/`을 제외하거나 EXE만 복사하면 실행할 수 없습니다. 공개용 묶음과 소스·허가문 패키지는 `tools/package_suite.py`의 안내와 프로젝트 README를 따릅니다.

Qt/PySide 등의 라이브러리 허가문과 대응 소스·교체 방법은 [DEPENDENCY_SOURCES.md](DEPENDENCY_SOURCES.md) 및 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)를 따릅니다.

## 이 버전에서 사용하지 않는 기존 도구

`scripts/build_native.py`, `scripts/assemble_release.py`, `scripts/run_offline_tests.py`, `scripts/verify_release.py`는 이전 카메라 드라이버·설치기 묶음용 절차입니다. 이 2.3 앱 소스에는 그 전체 입력이 포함되지 않으며, 통합 앱을 만들 때 실행하지 않습니다. sender 재빌드는 `build_sender.py`, 앱 빌드는 `build_app.py`, 통합 배포 준비는 `tools/package_suite.py`로 구분합니다.

빌드와 형식 검증은 실제 게임 인식·설치 시험을 대신하지 않습니다. 개발 환경의 `build/`, `.tools/`, 가상환경, 사용자 이미지·티켓·프로필·로그는 소스 배포에 포함하지 않습니다.
