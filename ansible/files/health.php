<?php
require '/var/www/otus/wp-load.php';
global $wpdb;
header('Content-Type: text/plain; charset=utf-8');
if (!is_blog_installed() || (string) $wpdb->get_var('SELECT 1') !== '1') {
    http_response_code(503);
    echo "unavailable\n";
    exit;
}
echo "ready\n";
