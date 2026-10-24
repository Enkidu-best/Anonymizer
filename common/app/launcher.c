// Исполняемый файл любого .app программ Андрея (стандарт 6.2): запускает launcher.sh и ждёт его.
// Почему не bash-скрипт: если исполняемый файл пакета - скрипт, macOS считает обращающейся к папке «Рабочий стол»
// программой /bin/bash, вопрос о доступе не показывает и молча отказывает - пакет «не открывается» (Interview Coach
// 23.10, «Видеокарта.app» 24.10). Своя маленькая программа - и macOS спрашивает про само приложение.
// Имя и путь к скрипту задаёт сборщик (build_app.sh): -DAPP_NAME="…" -DSCRIPT_REL="…" - путь от папки, где лежит
// пакет (или абсолютный). Вся логика - в launcher.sh (правится без пересборки).
#include <fcntl.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef APP_NAME
#define APP_NAME "Program"
#endif
#ifndef SCRIPT_REL
#define SCRIPT_REL "desktop/launcher.sh"
#endif

extern char **environ;

static void note(const char *log, const char *msg) {
    FILE *f = fopen(log, "a");
    if (!f) return;
    char ts[32];
    time_t t = time(NULL);
    strftime(ts, sizeof(ts), "%d.%m.%Y %H:%M:%S", localtime(&t));
    fprintf(f, "%s приложение: %s\n", ts, msg);
    fclose(f);
}

int main(void) {
    char exe[PATH_MAX], real[PATH_MAX], base[PATH_MAX], script[PATH_MAX], log[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) return 1;
    // .../Имя.app/Contents/MacOS/launcher -> папка, где лежит пакет (на четыре уровня выше)
    strncpy(base, real, sizeof(base));
    for (int i = 0; i < 4; i++) strncpy(base, dirname(base), sizeof(base));
    if (SCRIPT_REL[0] == '/') snprintf(script, sizeof(script), "%s", SCRIPT_REL);
    else snprintf(script, sizeof(script), "%s/%s", base, SCRIPT_REL);
    snprintf(log, sizeof(log), "%s/Library/Logs/%s.log", getenv("HOME") ? getenv("HOME") : "/tmp", APP_NAME);

    note(log, "запуск launcher.sh");
    int fd = open(script, O_RDONLY);  // macOS не дал доступ к папке - подсказка, как разрешить
    if (fd < 0) {
        note(log, "macOS не дал прочитать папку программы");
        char text[1024];
        snprintf(text, sizeof(text),
                 "display dialog \"macOS не дал приложению доступ к папке программы (обычно «Рабочий стол»).\n\n"
                 "Системные настройки → Конфиденциальность и безопасность → Файлы и папки → %s → "
                 "«Папка „Рабочий стол“». Затем запустите приложение ещё раз.\" with title \"%s\" "
                 "buttons {\"Закрыть\"} with icon caution", APP_NAME, APP_NAME);
        char *dlg[] = {"/usr/bin/osascript", "-e", text, NULL};
        pid_t d;
        if (posix_spawn(&d, dlg[0], NULL, NULL, dlg, environ) == 0) waitpid(d, NULL, 0);
        return 1;
    }
    close(fd);
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_addopen(&fa, 2, log, O_WRONLY | O_APPEND | O_CREAT, 0644);  // ошибки - в журнал
    char *argv[] = {"/bin/bash", script, NULL};
    pid_t pid;
    if (posix_spawn(&pid, "/bin/bash", &fa, NULL, argv, environ) != 0) return 1;
    int status = 0;
    waitpid(pid, &status, 0);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
