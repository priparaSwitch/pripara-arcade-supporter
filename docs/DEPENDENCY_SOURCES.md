# 라이브러리 소스와 교체

2.0 통합 앱은 교체 가능한 `_internal/` 라이브러리와 별도 허가문을 사용합니다. Qt/PySide 라이브러리는 수정하지 않은 6.12.0 wheel이며, 대응하는 원소스 다섯 개는 `PriParaImageCamera_QtSources_6.12.0.zip`으로 함께 보존합니다.

| 원소스 | SHA-256 |
| --- | --- |
| qtbase-everywhere-src-6.12.0.tar.xz | `a951bd163c7b80fc6b8c88d7668fb56abf91c152373e13c10666763238131307` |
| qtimageformats-everywhere-src-6.12.0.tar.xz | `0943132b5db8de6b6a7f1d162682173e8763c7ed94e80fd39113292845f67762` |
| qtsvg-everywhere-src-6.12.0.tar.xz | `e4ab39534ec97987b1b9b60ef7f4d3253d5912a69f8c713a01753174a4029331` |
| qttranslations-everywhere-src-6.12.0.tar.xz | `85929c0c30d6f273f23bd879bb69803ea17b010ab73cab2f8abf95357f6f6bbb` |
| pyside-setup-everywhere-src-6.12.0.tar.xz | `099c1a597cf33c1b000e11cf68a0b82d216fb802bf81726efc720394f10c7751` |

출처는 [Qt modules](https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/)와 [Qt for Python source](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/)입니다. `scripts/fetch_third_party_sources.py`에 원래 URL·크기·검증값이 있습니다.

앱을 닫은 뒤 `_internal/PySide6/`, `_internal/shiboken6/`와 필요한 플러그인을 동일한 Windows x64·Python ABI에 맞게 교체할 수 있습니다. 앱은 Qt DLL에 고정 해시를 강제하지 않습니다. LGPL 라이브러리 수정·교체와 그 조합의 디버깅을 제한하지 않습니다. 원문 허가문은 `licenses/Qt/`에 있습니다.

독립 송출 모듈은 `native/sender/PriParaFrameSender.cpp`, 참고한 프로토콜은 `native/qr/shared.inl`입니다. `scripts/build_sender.py`로 DLL을 재빌드하고 `scripts/build_app.py`로 앱을 묶습니다. 기존 QR 카메라 드라이버는 별도 구성요소이며 이 앱 업데이트에는 설치·삭제 작업이 없습니다.

전체 구성요소 안내는 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)를 따릅니다.
