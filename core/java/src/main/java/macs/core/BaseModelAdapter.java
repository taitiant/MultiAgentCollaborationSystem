package macs.core;
import java.util.Map;

public interface BaseModelAdapter {
    String getModelName();
    String generate(String prompt, Map<String, Object> context);
}
