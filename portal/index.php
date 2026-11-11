<!doctype html>
<html <?php language_attributes(); ?>>
<head>
  <meta charset="<?php bloginfo('charset'); ?>">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title><?php echo esc_html(wp_get_document_title()); ?></title>
  <link rel="stylesheet" href="<?php echo esc_url(get_stylesheet_uri()); ?>">
  <?php wp_head(); ?>
</head>
<body <?php body_class(); ?>>
<main>
  <header><a href="<?php echo esc_url(home_url('/')); ?>">OTUS</a><h1>Учебные проекты</h1></header>
  <?php if (is_singular()): ?>
    <?php while (have_posts()): the_post(); ?>
      <article><h2><?php the_title(); ?></h2><?php the_content(); ?></article>
    <?php endwhile; ?>
  <?php elseif (is_home()): ?>
    <?php
    $lessons = new WP_Query([
        'posts_per_page' => -1,
        'meta_key' => 'otus_lesson',
        'orderby' => 'meta_value_num',
        'order' => 'ASC',
    ]);
    ?>
    <section class="cards" aria-label="Материалы">
      <?php while ($lessons->have_posts()): $lessons->the_post(); ?>
        <article class="lesson-card">
          <span><?php echo esc_html(sprintf('ДЗ%02d', (int) get_post_meta(get_the_ID(), 'otus_lesson', true))); ?></span>
          <h2><a href="<?php the_permalink(); ?>"><?php the_title(); ?></a></h2>
          <?php the_excerpt(); ?>
        </article>
      <?php endwhile; wp_reset_postdata(); ?>
    </section>
  <?php else: ?>
    <p>Материал не найден.</p>
  <?php endif; ?>
</main>
<?php wp_footer(); ?>
</body>
</html>
