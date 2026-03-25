package macs.core;
import java.util.List;
import java.util.Map;

public record SystemState(
        Map<String, Task> tasks,
        Map<String, String> taskStatus,
        List<Event> history) {}
