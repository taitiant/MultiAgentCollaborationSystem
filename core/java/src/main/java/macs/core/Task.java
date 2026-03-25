package macs.core;
import java.util.List;
import java.util.Map;

public record Task(
        String taskId,
        String domain,
        List<String> requiredCapabilities,
        Map<String, Object> context,
        int priority,
        String workspacePath) {}
