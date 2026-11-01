#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    char *end = NULL;
    unsigned long seconds = strtoul(argv[1], &end, 10);
    if (end == argv[1] || *end != 0 || seconds > 300) return 2;
    while (seconds != 0) seconds = sleep((unsigned int)seconds);
    return 0;
}
