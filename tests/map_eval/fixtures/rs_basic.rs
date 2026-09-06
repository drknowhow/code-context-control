use std::collections::HashMap;
use std::fmt;

pub const MAX_ITEMS: usize = 128;

#[derive(Debug, Clone)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

pub enum Shape {
    Circle(Point, f64),
    Rect(Point, Point),
}

pub trait Area {
    fn area(&self) -> f64;
    fn label(&self) -> String;
}

impl Point {
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }

    pub fn dist(&self, other: &Point) -> f64 {
        ((self.x - other.x).powi(2) + (self.y - other.y).powi(2)).sqrt()
    }
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "({}, {})", self.x, self.y)
    }
}

pub fn centroid(points: &[Point]) -> Option<Point> {
    if points.is_empty() {
        return None;
    }
    let n = points.len() as f64;
    let (sx, sy) = points.iter().fold((0.0, 0.0), |(ax, ay), p| (ax + p.x, ay + p.y));
    Some(Point::new(sx / n, sy / n))
}

pub fn index_by_label(points: &[Point]) -> HashMap<String, Point> {
    points.iter().map(|p| (format!("{}", p), p.clone())).collect()
}
