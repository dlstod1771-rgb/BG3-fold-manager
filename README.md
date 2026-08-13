# BG3 Mod Bridge 2.0

DCInside 발더스 게이트 3 가이드에서 모드 출처를 수집하고, 다운로드 전 검토 계획과 출처 기록을 만드는 Windows용 관리 도구입니다. 기존 GUI 사용법은 유지하면서 다운로드 안전 검사와 명령줄 워크플로를 추가했습니다.

## 주요 개선점

- DCInside 내부 가이드를 깊이 3, 최대 40페이지까지 순회하며 방문 집합으로 순환 링크를 차단합니다.
- Nexus, Patreon, Google Drive, DCInside, 직접 링크를 구조적으로 분류합니다.
- 번호가 붙은 DCInside 시리즈는 안내글을 다운로드 표에 넣지 않고 1~30 각각의 컬렉션으로 생성합니다.
- 같은 Nexus 모드 또는 같은 출처 파일이 여러 컬렉션에 있으면 `중복 컬렉션` 열에 번호를 표시합니다.
- 왼쪽 `링크별 컬렉션 목차`에서 번호·이름·모드 수·중복 수 기준 정렬과 오름/내림차순 전환을 지원합니다.
- Shift 또는 Ctrl로 컬렉션을 여러 개 선택한 뒤 `선택 일괄삭제`를 사용할 수 있습니다.
- 검색 색인 줄 맨 앞의 `컬렉션 모드상황` 아래에서 적용·모드/링크·출처·중복·다운로드·상태를 확인합니다.
- `컬렉션 프로젝트` 상위 탭에서 게임, 추가 날짜시간, 목차 수, 총 모드 수, 공유모드 수를 확인합니다.
- 컬렉션 프로젝트는 라이브러리의 실제 상위 폴더이며 링크별 컬렉션 폴더들이 그 아래에 보관됩니다. 프로젝트 탭에서 선택·폴더 열기·삭제·정렬을 지원합니다.
- `공유모드` 상위 탭은 같은 프로젝트의 여러 목차에 중복된 항목을 통합 표시하고 선택 그룹을 `공유모드`로 관리합니다.
- 공유모드 차트에서 이름·출처·컬렉션을 검색하고 이름·출처·중복 수·연결 파일·컬렉션 기준으로 정렬할 수 있습니다.
- 공유모드 탭에서 다운로드, 원본 링크 열기, 컬렉션에서 보기, 연결 상태 새로고침을 지원합니다.
- 한 컬렉션에 연결된 공유 파일은 프로젝트의 `_공유모드\downloads`에 보관되고 같은 모드를 사용하는 모든 컬렉션에 자동 연결됩니다. 서로 다른 파일이 발견되면 자동 덮어쓰기 없이 `파일 충돌`로 표시합니다.
- 다운로드 파일명과 모드/링크 표시 이름은 분리됩니다. 모드 행 우클릭으로 이름을 변경하거나 최초 추출 이름으로 복원할 수 있습니다.
- `수동 파일 연결`은 현재 링크별 컬렉션의 `downloads` 폴더에서 시작하며 선택 파일도 그 폴더에 보관합니다.
- Nexus `mod_id`와 `file_id`는 URL에 실제로 있는 값만 기록합니다. 모드 페이지만으로 `file_id`를 추측하지 않습니다.
- 각 항목에 원본 가이드, 앵커 문구, 문맥, 정규화 URL, 출처 식별자를 남깁니다.
- 다운로드와 설치 선택을 분리하며 컬렉션에 재개 가능한 작업 상태를 저장합니다.
- 다운로드 전 `.part` 파일을 사용하고 완료 후 원자적으로 이동합니다.
- 크기, SHA-256, HTML 위장 파일, ZIP CRC, ZIP 경로 이탈 및 심볼릭 링크를 검사합니다.
- Nexus/Patreon 로그인, 유료 권한, CAPTCHA는 우회하지 않고 `browser_required` 또는 `needs_review`로 중단합니다.
- 기존 버전 2 컬렉션은 열 때 버전 3 스키마로 보존 마이그레이션합니다.

## GUI 실행

`BG3 Mod Bridge 실행.vbs`를 실행하면 CMD 창 없이 시작합니다. 기존 `BG3 Mod Bridge 실행.cmd`도 일반 실행 시 숨김 실행기로 즉시 넘기므로 콘솔 창을 유지하지 않습니다. `.pyw`는 `BG3ModBridge.py`를 호출하며 오류가 발생하면 `%LOCALAPPDATA%\BG3ModBridge\error.log`에 기록합니다.

GUI의 `다운로드 계획` 버튼은 실제 다운로드 없이 `download-plan.json`을 만들고, `환경 진단` 버튼은 `%LOCALAPPDATA%\BG3ModBridge\capabilities.json`을 만듭니다.

Kortex나 Vortex의 VFS는 설치와 파일 충돌을 관리하는 기능입니다. Nexus/Patreon의 다운로드 권한을 제공하지 않습니다. `doctor`는 현재 `nxm://` 처리 프로그램이 Kortex인지 Vortex인지도 확인합니다.

## 안전한 명령줄 흐름

```powershell
python bg3_cli.py doctor
python bg3_cli.py parse-guide "DCINSIDE_URL" --output collection.json
python bg3_cli.py plan collection.json
python bg3_cli.py download collection.json
```

마지막 명령은 기본적으로 dry-run이며 파일을 변경하지 않습니다. 검토 후 공개 직접 링크만 실행하려면 두 승인을 모두 명시합니다.

```powershell
python bg3_cli.py download collection.json --execute --approve-downloads
```

파일 하나를 별도로 검사할 수도 있습니다.

```powershell
python bg3_cli.py validate "C:\path\to\mod.zip"
```

## 상태 의미

- `planned`: 공개 파일 링크이며 실행 승인 전
- `running`: 다운로드 중이며 중단 후 컬렉션에서 상태 확인 가능
- `verified`: 해시와 파일 검증 완료
- `needs_review`: 근거가 부족하여 사람의 판단 필요
- `browser_required`: 사용자 로그인 또는 사이트 권한 확인 필요
- `failed`: 오류가 컬렉션에 기록됨

대안 모드는 모두 다운로드할 수 있지만 설치 시에는 같은 `alternative_group`에서 하나만 선택해야 합니다. 자동 의미 분류의 기본값은 `unknown`이며 근거 없는 필수/선택 판단을 하지 않습니다.

## 선택 도구

`doctor`는 `python`, `git`, `7z`, `gdown`, `nexus-cli`를 `where`에 해당하는 방식으로 확인합니다. 없는 도구 때문에 앱 전체가 실패하지는 않습니다. 모든 외부 명령은 인자 배열과 `shell=False`를 사용합니다.

## 테스트

```powershell
python -m unittest -v test_orchestrator_core.py
python BG3ModBridge.py --self-test
```

테스트에는 Nexus ID 비추측, HTML 위장 ZIP 거부, ZIP 경로 이탈 거부, 정상 ZIP 해시 검증, 기존 컬렉션 보존 마이그레이션이 포함됩니다.

## 보안 원칙

브라우저 쿠키 DB 추출, 토큰 생성, 로그인 가장, CAPTCHA/Cloudflare 우회, Nexus 대기시간 우회, 잠긴 Patreon 콘텐츠 접근은 구현하지 않습니다. 인증이 필요한 파일은 사용자가 사이트에서 직접 권한을 확인하고 내려받은 뒤 `수동 파일 연결`로 연결하십시오.
