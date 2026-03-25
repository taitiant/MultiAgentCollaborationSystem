package macs.client;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

public class MacsClient {
    private final HttpClient client = HttpClient.newHttpClient();
    private final String base;

    public MacsClient(String baseUrl) {
        this.base = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length()-1) : baseUrl;
    }

    public String createTask(String jsonBody) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(base + "/tasks"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(jsonBody))
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString()).body();
    }

    public String step(String taskId) throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(base + "/tasks/" + taskId + "/step"))
                .POST(HttpRequest.BodyPublishers.noBody())
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString()).body();
    }

    public String listTasks() throws IOException, InterruptedException {
        HttpRequest req = HttpRequest.newBuilder()
                .uri(URI.create(base + "/tasks"))
                .GET()
                .build();
        return client.send(req, HttpResponse.BodyHandlers.ofString()).body();
    }

    public String events(String taskId) throws IOException, InterruptedException {
        String url = base + "/events" + (taskId == null ? "" : ("?task_id=" + taskId));
        HttpRequest req = HttpRequest.newBuilder().uri(URI.create(url)).GET().build();
        return client.send(req, HttpResponse.BodyHandlers.ofString()).body();
    }
}
