#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <unistd.h>

void rm(char *path)
{
    struct stat sb;
    DIR *dp;
    struct dirent *ep;
    char fullpath[1024];

    if (stat(path, &sb) == -1)
    {
        printf("stat of %s failed\n", path);
        exit(-2);
    }

    if ((sb.st_mode & S_IFMT) == S_IFDIR)
    {
        printf("%s is a directory, recursing...\n", path);
        dp = opendir(path);
        if (!dp)
        {
            printf("opendir of %s failed\n", path);
            exit(-3);
        }
        while ((ep = readdir(dp)))
        {
            if (strcmp(ep->d_name, ".") == 0)
                continue;
            if (strcmp(ep->d_name, "..") == 0)
                continue;

            snprintf(fullpath, 1024, "%s/%s", path, ep->d_name);
            rm(fullpath);
        }
        closedir(dp);
        if (rmdir(path))
        {
            printf("rmdir of %s failed\n", path);
            exit(-4);
        }
    }
    else
    {
        printf("... removing %s\n", path);
        if (unlink(path))
        {
            printf("unlink of %s failed\n", path);
            exit(-5);
        }
    }
}

int main(int argc, char **argv)
{
    if (argc < 2)
    {
        printf("Usage: removes a single path recursively, passed as the only "
               "command line argument.\n");
        exit(-1);
    }

    rm(argv[1]);
    exit(0);
}