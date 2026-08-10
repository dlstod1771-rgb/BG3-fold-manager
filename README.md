# BG3 Mod Bridge v1.0.0

BG3 모드 관리 프로그램 - Vortex Mod Manager와 연동하여 로컬 저장 모드를 자동으로 관리합니다.

## 🎯 주요 기능

### 1. 모드 링크 자동 수집
- 안내 페이지(디시인사이드 발게3 마이너 갤러리 등)에서 모드 링크를 자동으로 수집
- 작성자의 설명과 코멘트를 함께 저장
- 각 링크별 출처(Nexus, Patreon, Google Drive 등) 자동 분류

### 2. 메타데이터 보존
- **작성자 설명**: 원본 안내 글의 코멘트 보존
- **링크 제목**: Nexus/Patreon 페이지 제목 자동 감지
- 특별한 설명이 없는 링크는 페이지 제목을 "모드/링크" 고정값으로 사용

### 3. 스마트 다운로드
- 다양한 소스(Nexus, Patreon, Google Drive, 직접 링크)에서 자동 다운로드
- 3회 자동 재시도 기능
- 브라우저 다운로드 폴더 자동 감지

### 4. Vortex 연동 설치
- 압축 모드를 Vortex에 자동 설치
- Vortex 연동 플러그인 설치로 직접 Enable/Disable 가능
- BG3 Mods 폴더의 .pak 파일 직접 관리

### 5. 선택 그룹 관리
- 여러 모드를 그룹화하여 동시에 하나만 적용 가능
- 헤어스타일, 얼굴, 의상 등 선택지를 명확하게 구분

## 💻 시스템 요구사항

- **Windows 10 이상**
- **Python 3.8 이상**
- **Baldur's Gate 3** 설치됨
- **Vortex Mod Manager** (선택사항, .pak 파일은 필수)

## 🚀 사용 방법

### 1단계: 프로그램 실행
```bash
BG3 Mod Bridge 실행.cmd
```

### 2단계: 경로 설정 (첫 실행 시)
- **컬렉션 보관 폴더**: 모드 모음을 저장할 폴더 선택
- **BG3 Mods 폴더**: `C:\Users\[사용자명]\AppData\Local\Larian Studios\Baldur's Gate 3\Mods`
- **Vortex.exe**: Vortex 설치 경로
- **Vortex staging 폴더**: Vortex 모드 staging 디렉토리

### 3단계: 안내 페이지 링크 입력
```
예: https://gall.dcinside.com/mgallery/board/view/?id=bg3&no=916407
```

### 4단계: "수집" 버튼 클릭
- 페이지의 모든 모드 링크와 설명이 자동으로 수집됨

### 5단계: 모드 다운로드 및 적용
- **링크 열기**: 브라우저에서 페이지 확인
- **다운로드 대기**: 선택한 파일을 브라우저에서 다운로드
- **☑ 적용/해제**: 체크박스로 모드 활성화/비활성화

## 📋 버전 히스토리

### v1.0.0 (2026-08-10)
- ✨ 작성자 코멘트 메타데이터 보존 기능
- ✨ Nexus/Patreon 페이지 제목 자동 감지 및 고정값 사용
- ✨ 특별한 설명 없는 링크의 스마트 제목 처리
- ✨ Vortex 연동 플러그인 통합
- 🐛 다운로드 후 파일명 보존 개선

## 🔧 고급 기능

### Vortex 연동 설치
Vortex 소유 모드를 이 프로그램에서 직접 Enable/Disable:
```
"Vortex 연동 설치" 버튼 클릭
→ Vortex 재시작
→ 체크박스로 직접 관리 가능
```

### 수동 파일 연결
다운로드한 모드 파일을 링크와 수동으로 연결:
```
1. 링크 선택
2. "수동 파일 연결" 버튼
3. 로컬 파일 선택
4. 자동으로 컬렉션에 추가됨
```

### 선택 그룹 지정
상호 배타적 모드 관리 (예: 헤어스타일 중 하나만 선택):
```
1. 같은 그룹의 모드 선택
2. "선택그룹 지정" 버튼
3. 그룹 이름 입력 (예: "선택: 헤어스타일")
4. 한 번에 하나만 적용 가능
```

## 📁 폴더 구조

```
📁 BG3 Mod Bridge
├── 📄 BG3 Mod Bridge 실행.cmd    (프로그램 시작)
���── 📄 BG3ModBridge.pyw            (메인 프로그램)
├── 📁 vortex-extension/           (Vortex 플러그인)
│   ├── 📄 index.js
│   └── 📄 info.json
└── 📁 chrome-extension/           (브라우저 확장 - 선택)
    └── ...
```

## 🐛 트러블슈팅

### "Python 3 was not found"
- Python 3.8 이상 설치 필요
- https://www.python.org/downloads/

### "Vortex.exe 경로를 지정하세요"
- 설정 버튼 → Vortex.exe 경로 지정
- 기본 설치: `C:\Program Files\Vortex\Vortex.exe`

### 다운로드 실패 (3회 재시도 후)
- Nexus/Patreon은 로그인 필요
- 브라우저 프로필에서 로그인 상태 확인
- Google Drive는 공개 공유 링크 확인

### "로그인 또는 다운로드 확인이 필요한 페이지입니다"
- 로그인이 필요한 서비스
- 브라우저에서 수동으로 다운로드 후 "수동 파일 연결" 사용

## 📝 주의사항

- 적용 중인 모드는 프로그램에서 자동으로 감지됨
- BG3 Mods 폴더의 외부 수정은 감시됨
- Vortex 재시작 후 변경사항 반영
- 컬렉션 폭더는 로컬에만 저장되고 클라우드 동기화 불가

## 🔐 데이터 보안

- 모든 데이터는 로컬에 저장: `%LOCALAPPDATA%\BG3ModBridge\`
- 원격 서버로 정보 전송 없음
- 수집된 모드 리스트는 개인 컴퓨터에만 보관

## 📧 문의 및 버그 리포트

GitHub Issues를 통해 버그 리포트 및 기능 요청:
- https://github.com/dlstod1771-rgb/BG3-fold-manager/issues

## 📄 라이선스

MIT License - 자유롭게 사용, 수정, 배포 가능

## 🙏 감사의 말

- Baldur's Gate 3 커뮤니티
- Vortex Mod Manager
- 발게3 마이너 갤러리 모드 작성자들
