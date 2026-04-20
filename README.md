# Dictation PDF Maker

영어 자막, 팟캐스트 스크립트, 듣기 대본으로 딕테이션 학습지를 빠르게 만들기 위한 간단한 도구입니다.

`.txt` 또는 `.srt` 파일을 올리거나 텍스트를 직접 붙여 넣으면, 빈칸이 들어간 문제지와 원문 답지를 바로 PDF로 만들 수 있습니다. 파일 업로드 모드에서는 여러 파일을 한 번에 넣고 ZIP으로 받을 수도 있습니다.

## 실행 방법

가장 간단한 방법:

```bash
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py
```

가상환경을 사용하고 싶다면:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py
```

실행 후 브라우저가 열리면 바로 사용할 수 있습니다.

## 사용 방법

1. `파일 업로드` 또는 `텍스트 붙여넣기`를 고릅니다.
2. 자막이나 스크립트를 넣습니다.
3. `단어별 빈칸` 또는 `문장 전체 빈칸`을 고릅니다.
4. 필요하면 `각 문장의 첫 단어 남기기`, `시간 표시하기` 옵션을 켭니다.
5. 미리보기를 확인한 뒤 다운로드합니다.

- 직접 입력: 문제지 PDF, 답지 PDF 다운로드
- 파일 업로드: 여러 파일도 가능하며 문제지 ZIP, 답지 ZIP 다운로드

## 활용 팁

- 유튜브 영어 자막이나 팟캐스트 transcript로 바로 딕테이션 자료를 만들 수 있습니다.
- [PodScripts](https://podscripts.co) 에서 에피소드 transcript를 복사해서 `텍스트 붙여넣기`로 사용하는 것도 편합니다.
- `PodScripts Transcript` 형식은 `Starting point is HH:MM:SS` 같은 시간 정보도 함께 처리합니다.
