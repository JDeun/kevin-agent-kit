# kevin-agent-kit

여러 코딩 에이전트의 구현, 리뷰, 통합을 로컬에서 조율하는 하네스입니다.

> one human holds the decision gate; an agent fleet does the rest

[English](../../README.md) · [저장소](https://github.com/JDeun/kevin-agent-kit.git)

사람이 작업 방향을 정하고, 검토된 설계안을 선택하며, 배포와 정리를 승인합니다.
에이전트는 작업을 조율하고 변경 사항을 검토하며, 격리된 워크트리에서 문제를 수정하고
검증 근거를 수집합니다.

## 새 컴퓨터에서 시작하기

### 1. CLI와 전역 규칙 설치

Git을 설치하고, 배포 CLI가 사용할 Python 3.11 이상을 `/usr/bin/python3`에 준비합니다.
실행기는 Python 격리 모드를 사용합니다. 설치한 명령의 선택 의존성은 시스템 Python에서
불러올 수 있어야 합니다. 실제 저장소에서 루프를 실행하려면
OpenSSL과 Bubblewrap의 비특권 네임스페이스를 사용할 수 있는 Linux가 필요합니다.
신뢰할 수 있는 키트 체크아웃에서 설치합니다. 기본 설치 경로는 `~/.local`입니다.

```sh
./scripts/install.sh
export PATH="$HOME/.local/bin:$PATH"
kevin-agent-kit --version
kevin-agent-kit demo
kevin-agent-kit init --global --dry-run
kevin-agent-kit init --global
```

Claude Code를 실행하는 셸에도 `PATH`를 설정합니다. 데모는 임시 Git 저장소와
내장 시험 명령을 사용합니다. 전역 설치는 `~/.claude/CLAUDE.md`와
`~/.codex/AGENTS.md`에 규칙 본문을 복사하고 설치 ID, 키트 버전과 본문 해시를 기록합니다.
새 버전을 승인한 뒤 `init --global`을 다시 실행하면 편집되지 않은 규칙을 갱신합니다.
소유권이나 내용 충돌이 있으면 두 파일 모두 쓰기 전에 중단합니다.
이전 링크형 또는 스냅샷 블록은 직접 검토한 뒤 `init --global --adopt-rules-sha256 SHA256`으로
갱신합니다. 명령이 표시한 정확한 해시를 사용하고, 해시가 여러 개면 옵션을 반복합니다.
[전역 규칙 소유권](../adr/global-rule-ownership.md)
홈 및 클라이언트 디렉터리는 현재 사용자가 소유해야 하며 심볼릭 링크와
그룹·기타 사용자 쓰기 권한은 허용하지 않습니다. 기존 파일을 갱신하려면 원자적
파일 교환과 메타데이터 보존을 지원하는 Linux 파일시스템이 필요합니다.
기존 파일은 키트의 XDG 상태 디렉터리에 백업합니다. 새 세션을 열면 규칙을 읽습니다.

Claude Code에서 마켓플레이스를 추가하고, 플러그인을 설치할 때 **User** 범위를 선택합니다.

```text
/plugin marketplace add JDeun/kevin-agent-kit
/plugin install kevin-agent-kit@kevin-agent-kit
```

로컬 체크아웃을 쓸 때는 마켓플레이스 추가 명령에 절대 경로를 넘깁니다.
`claude --plugin-dir ./plugin`으로 플러그인을 직접 불러올 수도 있습니다.
[Claude Code 마켓플레이스 안내](https://code.claude.com/docs/en/plugin-marketplaces)

### 2. 저장소별 설정 만들기

검토할 저장소에서 `kevin-agent-kit init`을 실행합니다. 출력된 TOML에서 감지된
대상 브랜치와 검증 명령 후보를 확인합니다. 렌즈별 리뷰 어댑터와 독립 검토·필수
검증의 서명 출처를 설정하고 어댑터별 번들 범위와 보존 기간을 승인합니다.
자동 수정에는 작업자 명령과 수정 경로를 승인합니다.
설정은 `${XDG_CONFIG_HOME:-$HOME/.config}/kevin-agent-kit/profiles/`에 생성됩니다.
[프로필과 래퍼 설정](../loop.md)

### 3. 루프 실행하기

변경을 커밋한 로컬 브랜치를 등록합니다. 다른 프로필을 쓰려면 하위 명령 앞에
`--config /절대/경로/profile.toml`을 지정합니다.

```sh
kevin-agent-kit start
kevin-agent-kit enqueue feature/example "$(git rev-parse refs/heads/feature/example)"
kevin-agent-kit status
```

`candidate` 작업 ID와 근거를 확인한 뒤
`kevin-agent-kit candidate JOB_ID --output /절대/경로/candidate.bundle`로
검증한 커밋과 그 커밋에서 도달할 수 있는 Git 이력을 로컬 파일로 내보냅니다.
적용은 프로젝트의 기존 승인·Git 절차를 따릅니다.
큐·로그·장부는 `${XDG_STATE_HOME:-$HOME/.local/state}/kevin-agent-kit/` 아래에 있습니다.

### 4. 루프 끄기와 전역 규칙 제거

```sh
kevin-agent-kit stop
kevin-agent-kit init --global --uninstall --dry-run
kevin-agent-kit init --global --uninstall
```

제거 명령은 설치 기록과 일치하는 관리 블록을 걷어냅니다. Claude Code 플러그인은
`/plugin uninstall kevin-agent-kit@kevin-agent-kit`으로 제거합니다.

키트 체크아웃에서는 `python3 -m core.loop`를 쓸 수 있습니다.
`./scripts/build.sh`로 설치 파일을 만들고, `./scripts/rename.sh NEW`로 빌드할
CLI와 플러그인 이름을 바꿀 수 있습니다. Python 모듈 이름은 밑줄을 사용합니다.
[아키텍처](../architecture.md) · [호스팅 어댑터](../hosting.md) · [알려진 이슈](../known-issues.md)

- [작업자 규칙](../../agents/worker-rules.md)은 브랜치 범위와 리소스 소유권을 다룹니다.
- [페르소나 리뷰](../../reviews/persona/README.md)는 설계 대안과 의사결정 자료를 만듭니다.
- [스크린샷 검증](../../verify/README.md)으로 실행 중인 앱을 캡처해 PR이나 MR에 근거를 남길 수 있습니다.
- [가드](../guards.md)와 [장부](../ledgers.md)에서 검사 항목과 기록 방법을 확인할 수 있습니다.
- [비용과 컨텍스트](../cost.md)는 모델 선택과 세션 사용량 관리 방법을 설명합니다.

## 참고한 공개 프로젝트와 발표

| 출처 | 참고한 아이디어 |
| --- | --- |
| [Lauren Tan, All Code Is Slop (Except Mine), AI Engineer](https://ai.engineer/talks/O_zcc6Xl12g-all-code-is-slop-except-mine) | 반복되는 리뷰 판단을 자동 검사로 옮기고, 실행 중인 앱에서 검증 근거 수집하기 |
| [cursor/plugins의 pstack](https://github.com/cursor/plugins/blob/main/pstack/README.md) | 사람이 선택하기 전에 독립적인 설계 대안을 만들고 문제점을 검토하기 |
| [ECC](https://github.com/affaan-m/ECC) | 재사용할 수 있는 에이전트 규칙과 도구를 작업 흐름과 함께 배포하기 |
| [AgentShield](https://github.com/affaan-m/agentshield) | 결정적 검사와 독립적인 리뷰 결과를 모아 구체적인 발견 사항을 정리하기 |

위 출처의 아이디어를 참고해 이 저장소의 코드와 문서를 처음부터 작성했습니다.
