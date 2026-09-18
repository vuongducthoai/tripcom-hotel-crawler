-- Remove text previously misclassified as a hotel description.

BEGIN;

UPDATE hotel_translations
SET description=NULL,
    updated_at=now()
WHERE description IS NOT NULL
  AND (
      lower(description) LIKE '%bạn đang tìm đặt phòng%'
      OR lower(description) LIKE '%hãy chọn phòng cho bạn%'
      OR lower(description) LIKE '%so sánh giá cả và đặt%'
      OR lower(description) LIKE '%chúng tôi khuyên bạn nên đặt%'
      OR lower(description) LIKE '%phải thanh toán thêm%'
      OR lower(description) LIKE '%looking to book%'
      OR lower(description) LIKE '%select rooms%'
      OR lower(description) LIKE '%compare prices and book%'
      OR lower(description) LIKE '%compare the latest room rates%'
  );

COMMIT;
