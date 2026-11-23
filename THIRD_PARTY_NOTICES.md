# Third-party notices

구성요소별 원래 저작권과 허가문을 유지합니다. 아래 안내는 프로젝트 전체에 임의의 MIT/GPL 라이선스를 부여하지 않습니다. 허가문 원문은 `licenses/`에 있습니다.

## UnityCapture 통신 형식과 독립 송출 모듈

공개 [UnityCapture](https://github.com/schellingb/UnityCapture) 프로토콜을 바탕으로 독립 작성한 `native/sender/PriParaFrameSender.cpp`와 `app/camera_output.py`를 사용합니다. 참고한 프로토콜 정의는 `native/qr/shared.inl`에 있으며 MIT 허가문을 보존합니다. 기반 소스 commit은 `3ed54c325e0ad71afcf4f246c07e5e17b3d7f2d2`입니다. 이 앱 패키지는 드라이버를 설치하거나 재등록하지 않습니다.

`pyvirtualcam`, GPL 백엔드, libyuv 코드를 복사하거나 번들하지 않습니다. 이전 구성요소의 원문 허가문이 보존되어 있어도 해당 구성요소를 이번 실행 파일에 포함했다는 뜻은 아닙니다.

## Qt / Qt for Python 6.12.0

PySide6-Essentials와 Shiboken6를 LGPLv3 조건으로 사용합니다. 상용 Qt 라이선스를 취득했다고 표시하지 않습니다. 허가문과 예외·제3자 안내는 `licenses/Qt/`에 있습니다.

Qt Core·Gui·Widgets·Network·OpenGL·SVG만 사용하며 PDF/QML/Quick/VirtualKeyboard는 제외합니다. `_internal/`의 동적 라이브러리는 교체할 수 있고, 수정한 조합을 디버깅하기 위한 역공학을 제한하지 않습니다. 대응하는 원소스는 별도 `PriParaImageCamera_QtSources_6.12.0.zip`에 보존합니다. 상세 버전·출처·교체 경로는 [DEPENDENCY_SOURCES.md](docs/DEPENDENCY_SOURCES.md)에 있습니다.

## QR 이미지

ZXing-C++ / zxing-cpp 3.1.1을 QR 픽셀 해석과 생성에 사용합니다. Apache-2.0 허가문과 NOTICE는 `licenses/zxing-cpp/`에 있습니다. 이 라이브러리는 PriPara 전용 암호화 키를 제공하지 않습니다.

## 선택적인 한국판 변환

cryptography 50.0.2와 cffi 2.1.1, pycparser 3.11, typing_extensions 4.16.0을 사용합니다. 원문 허가문은 각 버전의 `licenses/` 하위 폴더에 보존합니다. cryptography의 Apache-2.0/BSD 선택 허가문도 유지합니다. 배포 라이브러리는 범용 암호 알고리즘 구현이며 게임의 복호화 키를 제공하지 않습니다. 별도 로컬 자료가 없으면 한국판 변환을 사용할 수 없고 다른 기능은 실행됩니다.

## Python / Pillow / NumPy / 빌드 도구

| 구성요소 | 버전 | 원문 |
| --- | --- | --- |
| CPython | 3.10 계열 | `licenses/Python-LICENSE.txt` |
| Pillow | 12.3.0 | `licenses/pillow-12.3.0.dist-info/` |
| NumPy | 2.2.6 | `licenses/numpy-2.2.6.dist-info/` |
| PyInstaller | 6.22.3 | `licenses/pyinstaller-6.22.3.dist-info/` |
| LLVM-MinGW 런타임 | 20261020 UCRT | `licenses/llvm-mingw-LICENSE.txt` |

PyInstaller 원문의 배포 예외도 보존합니다. 그 예외를 프로젝트 전체에 GPL을 부여하는 것으로 해석하지 않습니다. Pillow·NumPy가 제공하는 하위 구성요소의 허가문도 함께 보존합니다.

## 앱 데이터와 배포 범위

의상 데이터와 이미지는 분리된 `data.db`·`images.bin`으로 공급됩니다. 원본 게임 실행 파일이나 개인 계정·티켓·이미지·세이브는 포함하지 않습니다. 사용자 프로필·티켓 색인·최근 목록·IPC·로그는 실행 후에 로컬로 생성되며 배포 자료에서 제외됩니다. 게임 자산에 대한 별도의 소유권을 주장하지 않습니다.

캐릭터 생성 탭의 파츠 명칭·선택 이미지·공개 고정 시작 QR은 기존 마이캐릭터 웹앱 자료를 재사용합니다. 고정 시작 QR은 빈 합성 테스트 데이터가 아니며, 사용자 요청에 따라 기존 공개 이미지를 유지합니다.

## 얼굴 미리보기

기존 아틀리에의 모델 조합 렌더러와 Three.js를 사용합니다. Three.js의 MIT 허가문은 `images.bin` 안 `_portrait/web/vendor/`에 보존합니다. 모델·텍스처는 원본 게임 자산의 파생 자료이며 별도의 소유권을 주장하지 않습니다. UnityPy는 준비 도구에서만 사용하고 실행 파일에는 포함하지 않습니다. 시스템 Edge 또는 Chrome을 별도의 임시 프로필로 실행하며 브라우저 자체는 배포하지 않습니다.
