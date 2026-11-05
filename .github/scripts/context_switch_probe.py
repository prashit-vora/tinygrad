def test_duplicate_tqdm_perf(self):
    import resource
    before = resource.getrusage(resource.RUSAGE_SELF)
    cpu_start = time.thread_time()
    wall_start = time.perf_counter()

    for _ in tqdm(range(100)):
        pass

    wall_elapsed_tqdm = time.perf_counter() - wall_start
    cpu_elapsed_tqdm = time.thread_time() - cpu_start
    after = resource.getrusage(resource.RUSAGE_SELF)

    voluntary_tqdm = after.ru_nvcsw - before.ru_nvcsw
    involuntary_tqdm = after.ru_nivcsw - before.ru_nivcsw

    before = resource.getrusage(resource.RUSAGE_SELF)
    cpu_start = time.thread_time()
    wall_start = time.perf_counter()

    for _ in tinytqdm(range(100)):
        pass

    wall_elapsed_tiny = time.perf_counter() - wall_start
    cpu_elapsed_tiny = time.thread_time() - cpu_start
    after = resource.getrusage(resource.RUSAGE_SELF)

    voluntary_tiny = after.ru_nvcsw - before.ru_nvcsw
    involuntary_tiny = after.ru_nivcsw - before.ru_nivcsw

    print(f"tqdm wall time:             {wall_elapsed_tqdm}")
    print(f"tqdm cpu time:              {cpu_elapsed_tqdm}")
    print(f"tqdm voluntary switches:    {voluntary_tqdm}")
    print(f"tqdm involluntary switches: {involuntary_tqdm}")

    print(f"tiny wall time:             {wall_elapsed_tiny}")
    print(f"tiny cpu time:              {cpu_elapsed_tiny}")
    print(f"tiny voluntary switches:    {voluntary_tiny}")
    print(f"tiny involluntary switches: {involuntary_tiny}")

    print('----')
    print()

    assert wall_elapsed_tiny < 5 * wall_elapsed_tqdm
