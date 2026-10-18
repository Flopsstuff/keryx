"""Impostor voices for voice ID: jokes and odd remarks in every xAI TTS voice, for wakeword/replay.py to play through
the MacBook and record through the board.

Every voice says PER_VOICE phrases (mostly Russian, some English), at a random speed and now and then in a speech tag
style. Clips go to data/tts-impostors/ as 16 kHz WAV with a manifest.jsonl in generate.py's format, so:

    cd wakeword && .venv/bin/python replay.py ../voiceid/data/tts-impostors --label imp-xai --count 1000

Run with the wake word's environment (it has aiohttp): wakeword/.venv/bin/python voiceid/impostors.py
"""

import argparse
import asyncio
import json
import random
import sys
import wave
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent / "wakeword"))
from xai_tts import RATE, XAITTS, styled  # noqa: E402

PER_VOICE = 8
STYLES = {"none": 0.7, "soft": 0.06, "loud": 0.06, "whisper": 0.06, "fast": 0.06, "slow": 0.06}

RU = [
    "Я не ленивый, я просто в режиме энергосбережения.",
    "Кот опять сел на клавиатуру и отправил начальнику письмо из одних букв «ж».",
    "Слушай, а почему холодильник всегда пустой, если я его каждые пять минут проверяю?",
    "Диета — это когда ешь то же самое, но с чувством вины.",
    "Будильник прозвенел, я его выключил. Будильник проиграл, я выиграл ещё пять минут.",
    "Говорят, утро вечера мудренее. Но вечером хотя бы не надо вставать.",
    "Я поставил пароль «неправильный», теперь компьютер сам мне его подсказывает.",
    "Сегодня я был продуктивен: дважды открыл холодильник и один раз закрыл.",
    "Купил тренажёр. Теперь на нём отлично сохнет бельё.",
    "А давай закажем пиццу и сделаем вид, что это был салат?",
    "Мой план на выходные: ничего не делать и не успеть.",
    "Если долго смотреть на кофеварку, кофе не сварится быстрее. Я проверял.",
    "Интернет отключили, пришлось поговорить с семьёй. Вроде нормальные люди.",
    "Я не опаздываю, я просто прихожу в самый драматичный момент.",
    "Почему носки всегда теряются по одному? Где-то живёт счастливый одноногий человек.",
    "Позвони маме, пока она сама не позвонила и не спросила, почему ты не звонишь.",
    "Кто опять съел последнее печенье и положил пустую пачку обратно?",
    "Сосед снова сверлит. Похоже, он строит тоннель прямо ко мне в гостиную.",
    "Я начну бегать с понедельника. Какого — пока не решил.",
    "Включи свет, я наступил на что-то мягкое и надеюсь, что это тапок.",
    "Мне кажется, наш робот-пылесос меня избегает.",
    "Подожди, я забыл, зачем пришёл на кухню. Ладно, раз уж пришёл, съем бутерброд.",
    "Сколько можно спрашивать про погоду? Посмотри в окно, там всё видно.",
    "Если что, я не сплю, я просто проверяю веки изнутри.",
    "Давай честно: эти цветы на подоконнике живут исключительно на силе воли.",
    "Вчера пытался приготовить ужин. Пожарная сигнализация оценила на пять звёзд.",
    "Я сказал жене, что она слишком рисует брови. Она удивилась.",
    "Где пульт? Он был тут секунду назад, а теперь в параллельной вселенной.",
    "Сделай погромче, это моя любимая песня, хотя я не знаю ни одного слова.",
    "Я не храплю, я просто во сне разговариваю с медведями.",
    "Кто придумал понедельник, тот явно не любил людей.",
    "Отличная новость: на улице снег. Плохая новость: машину откапывать мне.",
    "Сейчас бы на море, а не вот это вот всё.",
    "Мне нужен отпуск, чтобы отдохнуть после отпуска.",
    "Пробки девять баллов. Пойду пешком, всё равно быстрее.",
    "Ребята, кто оставил включённый утюг? А, это я, и он не включён. Расходимся.",
    "У меня всё под контролем. Просто контроль сейчас в отпуске.",
    "Я посчитал калории в торте и решил, что математика — это не моё.",
    "Мы же договаривались: кто последний встал, тот и варит кофе.",
    "Ты слышал, что ученые изобрели машину времени? Обещают показать её вчера.",
    "А какой сегодня день? Неважно, главное, что не понедельник. Ой, понедельник.",
    "Мне кажется, кот понимает русский, просто делает вид, что нет.",
    "Положи, пожалуйста, ключи туда, где мы их потом точно не найдём.",
    "Это не беспорядок, это творческий хаос с элементами системы.",
    "Если я не отвечаю, значит, я в домике.",
    "Слушай, ну хватит уже напоминать, я помню, что ничего не сделал.",
    "Я собирался встать пораньше, но одеяло было против.",
    "Кто-нибудь видел мои очки? Ах да, они у меня на голове.",
    "Давай без паники. Паника будет по расписанию, в пять вечера.",
    "Мне бы такую уверенность, как у голубя, который переходит дорогу.",
    "Интересно, а инопланетяне тоже спорят, кто моет посуду?",
    "Я решил стать утренним человеком. Утро не согласилось.",
    "Тише, тише, ребёнок наконец уснул. Кажется, это был я.",
    "Почему все хорошие идеи приходят в душе, где их негде записать?",
    "Сегодня по плану только кофе, сериал и философские размышления о смысле дивана.",
    "Включи что-нибудь весёлое, а то я уже начал разговаривать с цветами.",
    "Хочу сказать спасибо себе вчерашнему за то, что оставил кусок пиццы.",
    "А можно мне такой же рабочий график, как у кота?",
    "Я не спорю, я просто объясняю, почему я прав.",
    "Сколько раз говорить: зарядка от телефона — это не скакалка.",
    "Знаешь, что общего у меня и батарейки? Мы оба садимся к вечеру.",
    "Срочно нужна помощь: пельмени закончились, а голод нет.",
    "Сосед купил барабанную установку. Видимо, тишина ему надоела.",
    "Это был мой последний шанс, но я его тоже съел.",
    "Говорят, деньги не главное. Но почему-то без них главное не покупается.",
    "Если бы лень была спортом, я бы занял второе место. Лень было идти за первым.",
    "Ну всё, я обиделся. Минуты на три, потом пойдём пить чай.",
    "Я долго думал, кем стать. Пока думал, стал взрослым.",
    "Мой внутренний голос сегодня молчит. Видимо, тоже устал.",
    "Скажи честно, этот свитер меня полнит или это пирожки?",
]

EN = [
    "I told my wife she was drawing her eyebrows too high. She looked surprised.",
    "My plants are thriving on pure willpower and the occasional apology.",
    "I'm not arguing, I'm just explaining why I'm right.",
    "The early bird can have the worm. I'll take the coffee.",
    "Why do socks always disappear one at a time?",
    "I'm on a seafood diet. I see food, and I eat it.",
    "My bed is a magical place where I suddenly remember everything I forgot to do.",
    "I would lose weight, but I hate losing.",
    "Somebody ate the last cookie and put the empty box back in the cupboard.",
    "Can you turn the music down? The neighbors are starting to sing along.",
    "I don't snore. I dream that I'm a motorcycle.",
    "I was going to go running today, but the couch made a very convincing argument.",
    "Did you see where I put my keys? Never mind, they're in the fridge again.",
    "Monday is a terrible way to spend one seventh of your life.",
    "Let's order pizza and pretend it was a salad.",
    "My cat has a better sleep schedule than I do.",
    "I need a vacation to recover from my vacation.",
    "The robot vacuum is clearly avoiding me.",
    "If you stare at the kettle long enough, it still won't boil any faster.",
    "Honestly, I'm not late. Everyone else is just early.",
]


async def run(out, per_voice, seed):
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.jsonl"
    done = {json.loads(line)["id"] for line in open(manifest)} if manifest.exists() else set()
    async with XAITTS() as tts:
        voices = sorted(v["voice_id"] for v in await tts.voices())
        jobs = []
        for voice in voices:
            texts = rng.sample(RU, per_voice - 2) + rng.sample(EN, 2)
            for n, text in enumerate(texts):
                language = "en" if text in EN else "ru"
                style = rng.choices(list(STYLES), weights=list(STYLES.values()))[0]
                jobs.append({"group": "imp", "id": f"{voice}_{n:02d}", "text": text, "voice": voice,
                             "language": language, "speed": round(rng.uniform(0.9, 1.15), 2), "style": style})
        jobs = [j for j in jobs if j["id"] not in done]
        print(f"{len(voices)} voices, {len(jobs)} clips to make", flush=True)

        async def one(job):
            pcm, _ = await tts.synthesize(styled(job["text"], job["style"]), job["voice"], job["language"], job["speed"])
            with wave.open(str(out / f"{job['group']}_{job['id']}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(pcm)
            with open(manifest, "a") as f:
                f.write(json.dumps(job, ensure_ascii=False) + "\n")

        await asyncio.gather(*(one(j) for j in jobs))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=HERE / "data" / "tts-impostors")
    parser.add_argument("--per-voice", type=int, default=PER_VOICE)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(run(args.out, args.per_voice, args.seed))


if __name__ == "__main__":
    main()
